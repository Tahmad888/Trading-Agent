"""Opt-in bounded earnings retrieval/cache and explicit catalyst review. No orders."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Annotated

from pydantic import AwareDatetime, Field, model_validator

from desk.earnings import Record, Text, EarningsEvidence, ReviewedEarningsSource
from desk.earnings_check import aware_time
from desk.earnings_normalize import Archive, normalize, timestamp
from desk.earnings_sources import collect, save
from desk.catalysts import collect_candidates, reviewed_candidates
from desk.sec import SecData
from desk.symbols import canonical_symbol


class Identity(Record):
    symbol: Annotated[str, Field(pattern=r'^[A-Z0-9][A-Z0-9.-]{0,19}$')]
    security_id: Text
    cik: Annotated[str, Field(pattern=r'^\d{10}$')]
    issuer_evidence: str | None = None


class RefreshPolicy(Record):
    schema_version: int = 1
    review_ref: Text
    valid_until: AwareDatetime
    refresh_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)]
    retry_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)]
    catalyst_lookback_days: Annotated[int, Field(strict=True, ge=1, le=90)]
    catalyst_limit: Annotated[int, Field(strict=True, ge=1, le=10)]
    symbols: Annotated[tuple[Identity, ...], Field(min_length=1, max_length=5)]
    catalyst_reviews: str | None = None

    @model_validator(mode='after')
    def unique(self):
        if self.schema_version != 1 or len({i.symbol for i in self.symbols}) != len(self.symbols):
            raise ValueError('unsupported policy or duplicate identities')
        if len({i.security_id for i in self.symbols}) != len(self.symbols) or any(int(i.cik) == 0 for i in self.symbols):
            raise ValueError('invalid identities')
        return self


def load_policy(path):
    path = Path(path).resolve()
    raw = path.read_bytes()
    policy = RefreshPolicy.model_validate_json(raw)
    digest = hashlib.sha256(raw)
    for name in [policy.catalyst_reviews, *(s.issuer_evidence for s in policy.symbols)]:
        if name:
            digest.update(name.encode())
            digest.update((path.parent / name).read_bytes())
    return policy, digest.hexdigest()


class EarningsCache:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, at TEXT NOT NULL, expires TEXT NOT NULL, retry_at TEXT NOT NULL, policy_hash TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL)')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def lock(self):
        # One refresh per cache, across processes; readers still see latest committed state.
        with self.path.with_suffix(self.path.suffix + '.lock').open('a') as stream:
            os.chmod(stream.name, 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def append(self, symbol, at, expires, retry_at, policy_hash, status, payload):
        encoded = json.dumps(payload, allow_nan=False)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT at FROM snapshots WHERE symbol=? ORDER BY id DESC LIMIT 1', (symbol,)).fetchone()
            if previous and at < timestamp(previous[0]):
                raise ValueError('cannot replace newer snapshot with historical replay')
            db.execute('INSERT INTO snapshots(symbol,at,expires,retry_at,policy_hash,status,payload) VALUES (?,?,?,?,?,?,?)',
                       (symbol, at.isoformat(), expires.isoformat(), retry_at.isoformat(), policy_hash, status, encoded))

    def latest(self, symbol):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM snapshots WHERE symbol=? ORDER BY id DESC LIMIT 1', (symbol,)).fetchone()
        return dict(row) if row else None

    def due(self, symbol, at, policy_hash):
        row = self.latest(symbol)
        return row is None or row['policy_hash'] != policy_hash or at >= timestamp(row['retry_at'])

    def evidence(self, symbol, at, policy_hash):
        row = self.latest(canonical_symbol(symbol))
        if not row or row['policy_hash'] != policy_hash or row['status'] != 'READY':
            raise ValueError('current earnings snapshot unavailable')
        if not timestamp(row['at']) <= at < timestamp(row['expires']):
            raise ValueError('earnings snapshot stale or from future')
        return EarningsEvidence.model_validate(json.loads(row['payload'])['evidence'])


class CachedEarningsSource:
    """Cache read on every qualification; provider I/O belongs to explicit refresh."""
    def __init__(self, source, cache_path, policy_path):
        self.source, self.cache = source, EarningsCache(cache_path)
        self.policy_path = Path(policy_path)

    def __getattr__(self, name):
        return getattr(self.source, name)

    def earnings_evidence_at(self, symbol, at):
        policy, digest = load_policy(self.policy_path)
        if at > policy.valid_until:
            raise ValueError('earnings source policy expired')
        return self.cache.evidence(symbol, at, digest)


def refresh(policy_path, cache_path, output_dir, *, webull=None, sec=None,
            archive_dir=None, at=None, clock=lambda: datetime.now(timezone.utc)):
    """One bounded batch; cache failures as well as successes; never renew manual reviews."""
    policy_path = Path(policy_path).resolve()
    policy, digest = load_policy(policy_path)
    start = at or clock()
    if start > policy.valid_until:
        raise ValueError('refresh policy expired')
    cache = EarningsCache(cache_path)
    with cache.lock():
        due = [i for i in policy.symbols if cache.due(i.symbol, start, digest)]
        result = dict(purpose='earnings refresh only; no scanner/order activation',
                      mode='archive replay' if archive_dir else 'provider refresh', checks=[], cached=[])
        result['cached'] = [i.symbol for i in policy.symbols if i not in due]
        result['cached_status'] = {symbol: cache.latest(symbol)['status'] for symbol in result['cached']}
        if not due:
            result['status'] = 'CACHED' if all(v == 'READY' for v in result['cached_status'].values()) else 'INCOMPLETE'
            return result
        retry = start + timedelta(seconds=policy.retry_seconds)
        for identity in due:
            cache.append(identity.symbol, start, start, retry, digest, 'REFRESHING', {})
        try:
            if archive_dir is None:
                if webull is None or sec is None:
                    raise ValueError('providers required')
                observation_report = collect([i.symbol for i in due], output_dir, webull=webull, sec=sec, clock=clock)
                archive_dir = observation_report['run_directory']
            mapped_at = at or clock()
            review_rows = []
            if policy.catalyst_reviews:
                review_data = json.loads((policy_path.parent / policy.catalyst_reviews).read_text())
                if review_data.get('schema_version') != 1 or not isinstance(review_data.get('reviews'), list):
                    raise ValueError('invalid catalyst reviews')
                review_rows = review_data['reviews']
        except Exception:
            failed_at = at or clock()
            for identity in due:
                cache.append(identity.symbol, failed_at, failed_at, failed_at + timedelta(seconds=policy.retry_seconds),
                             digest, 'UNAVAILABLE', {'reason': 'retrieval or review configuration failed'})
            return {**result, 'status': 'UNAVAILABLE'}
        document_source_failed = False
        for identity in due:
            try:
                issuer = None
                if identity.issuer_evidence:
                    issuer = ReviewedEarningsSource(None, policy_path.parent / identity.issuer_evidence).earnings_evidence(identity.symbol)
                mapped = normalize(archive_dir, symbol=identity.symbol, security_id=identity.security_id, cik=identity.cik,
                                   review_ref=policy.review_ref, reviewed_at=mapped_at,
                                   valid_until=min(mapped_at + timedelta(seconds=policy.refresh_seconds), policy.valid_until), issuer=issuer)
                archived = Archive(archive_dir, mapped_at)
                submissions = archived.get('sec', identity.symbol, 'submissions')
                # SEC document fetching is optional only in archive replay; no fake healthy empty report.
                if sec is None or document_source_failed:
                    candidates = {'coverage': 'SEC_FILINGS_ONLY', 'status': 'NOT_FETCHED', 'candidates': []}
                else:
                    candidates = collect_candidates(sec, submissions, symbol=identity.symbol, security_id=identity.security_id,
                        cik=identity.cik, at=mapped_at, lookback_days=policy.catalyst_lookback_days, limit=policy.catalyst_limit)
                    document_source_failed = candidates['status'] == 'UNAVAILABLE'
                finished = at or clock()
                # Existing content-bound reviews can accept only exactly these public documents.
                relevant = [r for r in review_rows if r.get('symbol') == identity.symbol]
                accepted, review_status = reviewed_candidates(candidates['candidates'], relevant, finished)
                if candidates['status'] != 'AVAILABLE':
                    accepted = []  # A partial retrieval cannot silently retain catalyst eligibility.
                evidence = EarningsEvidence.model_validate(mapped['evidence'])
                oldest_receipt = min(timestamp(row['received_at']) for row in mapped['source_files'])
                expires = min(evidence.valid_until, policy.valid_until,
                              oldest_receipt + timedelta(seconds=policy.refresh_seconds),
                              *(until for _, until in accepted))
                evidence = EarningsEvidence.model_validate({**evidence.model_dump(), 'reviewed_at': finished,
                    'valid_until': expires, 'catalysts': [c for c, _ in accepted]})
                payload = {**mapped, 'evidence': evidence.model_dump(mode='json'), 'catalyst_collection': candidates,
                           'catalyst_reviews': review_status, 'archive_directory': str(archive_dir)}
                healthy = all(mapped['source_health'].get(k) == 'AVAILABLE'
                              for k in ('earnings_calendar', 'submissions', 'companyfacts'))
                # No announced future date is a valid unknown, not a source outage.
                healthy = healthy and mapped['source_health'].get('financial_alert') in {'AVAILABLE', 'EMPTY'}
                status = 'READY' if mapped['status'] == 'MAPPED' and healthy and expires > finished else 'INCOMPLETE'
                # Configuration edits during network work invalidate this attempt.
                if load_policy(policy_path)[1] != digest:
                    raise ValueError('review configuration changed during retrieval')
                cache.append(identity.symbol, finished, expires, min(expires, finished + timedelta(seconds=policy.refresh_seconds))
                             if status == 'READY' else finished + timedelta(seconds=policy.retry_seconds), digest, status, payload)
                result['checks'].append(dict(symbol=identity.symbol, status=status, mode=mapped['mode'],
                    expires_at=expires.isoformat(), issues=mapped['issues'], metric_coverage=mapped['metric_coverage'],
                    catalyst_status=candidates['status'], candidates=len(candidates['candidates']), accepted_catalysts=len(accepted)))
            except Exception:
                finished = at or clock()
                cache.append(identity.symbol, finished, finished, finished + timedelta(seconds=policy.retry_seconds),
                             digest, 'UNAVAILABLE', {'reason': 'mapping, source age or catalyst review failed'})
                result['checks'].append({'symbol': identity.symbol, 'status': 'UNAVAILABLE'})
        result['status'] = 'READY' if all(r['status'] == 'READY' for r in result['checks']) and all(
            v == 'READY' for v in result['cached_status'].values()) else 'INCOMPLETE'
        return result



def failed_refresh(policy_path, cache_path, at):
    policy, digest = load_policy(policy_path)
    cache = EarningsCache(cache_path)
    with cache.lock():
        for identity in policy.symbols:
            cache.append(identity.symbol, at, at, at + timedelta(seconds=policy.retry_seconds), digest,
                         'UNAVAILABLE', {'reason': 'provider configuration unavailable'})


def refresh_configured(webull, env):
    if env.get('DESK_EARNINGS_AUTO_REFRESH') != '1':
        return None
    policy, database = env.get('DESK_EARNINGS_POLICY'), env.get('DESK_EARNINGS_CACHE')
    if not policy or not database or env.get('DESK_EARNINGS_EVIDENCE'):
        raise ValueError('automatic refresh requires unambiguous cache/policy configuration')
    at = datetime.now(timezone.utc)
    config, digest = load_policy(policy)
    cache = EarningsCache(database)
    if at > config.valid_until:
        return {'status': 'POLICY_EXPIRED'}
    if not any(cache.due(i.symbol, at, digest) for i in config.symbols):
        return {'status': 'CACHED' if all(cache.latest(i.symbol)['status'] == 'READY' for i in config.symbols) else 'INCOMPLETE'}
    try:
        sec = SecData(env.get('SEC_USER_AGENT', ''))
    except ValueError:
        failed_refresh(policy, database, at)
        return {'status': 'UNAVAILABLE'}
    try:
        return refresh(policy, database, env.get('DESK_EARNINGS_ARCHIVES', str(Path(database).parent / 'earnings-archives')),
                       webull=webull, sec=sec)
    except BlockingIOError:
        return {'status': 'REFRESH_IN_PROGRESS'}

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--archive', help='reuse full archive; no providers called')
    parser.add_argument('--at', type=aware_time, help='required historical clock for archive replay')
    parser.add_argument('--report', required=True, help='new report filename')
    args = parser.parse_args(argv)
    if bool(args.archive) != bool(args.at):
        parser.error('--archive and --at must be supplied together')
    try:
        if Path(args.report).exists():
            raise ValueError('report exists')
        sec = webull = None
        if not args.archive:
            from desk.webull import WebullData
            try:
                webull = WebullData.from_env()
                ua = os.environ.get('SEC_USER_AGENT')
                if not ua:
                    from getpass import getpass
                    ua = getpass('SEC application/contact (hidden, not saved): ')
                sec = SecData(ua)
            except Exception:
                failed_refresh(args.policy, args.database, datetime.now(timezone.utc))
                raise
        result = refresh(args.policy, args.database, args.output_dir, webull=webull, sec=sec,
                         archive_dir=args.archive, at=args.at)
        save(Path(args.report), result)
    except Exception:
        result = {'status': 'UNAVAILABLE', 'reason': 'refresh configuration, lock or output unavailable'}
    print(json.dumps(result, indent=2))
    return 0 if result['status'] in {'READY', 'CACHED'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
