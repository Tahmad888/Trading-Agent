"""Original SEC filing candidates and explicit content-bound reviews, not sentiment."""
from datetime import date, timedelta
import hashlib
import json
import re

from pydantic import AwareDatetime, Field
from typing import Annotated, Literal

from desk.calendar import clock, trading_day, session, next_trading_day
from desk.earnings import Record, Text, Catalyst
from desk.earnings_normalize import timestamp
from desk.symbols import canonical_symbol


class CatalystReview(Record):
    document_id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    symbol: Text
    security_id: Text
    decision: Literal['accepted', 'rejected']
    reviewed_at: AwareDatetime
    valid_until: AwareDatetime
    kind: Literal['earnings', 'other']
    summary: Text
    relevance_ref: Text
    report_period_end: date | None = None


def entry_session(published_at):
    at = clock(published_at)
    day = at.date()
    return day if trading_day(day) and at < session(day)[1] else next_trading_day(day)


def document_id(item):
    fields = {k: item[k] for k in ('symbol', 'security_id', 'cik', 'accession', 'form', 'items', 'source_url', 'published_at', 'body_sha256')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def collect_candidates(sec, submissions, *, symbol, security_id, cik, at, lookback_days, limit):
    """8-K filings are review candidates; even Item 2.02 does not auto-qualify."""
    if not 1 <= lookback_days <= 90 or not 1 <= limit <= 10:
        raise ValueError('invalid bounded candidate request')
    data = submissions['payload']
    if str(data['cik']).lstrip('0') != cik.lstrip('0') or symbol not in {canonical_symbol(s) for s in data['tickers']}:
        raise ValueError('candidate identity mismatch')
    recent = data['filings']['recent']
    keys = ('accessionNumber', 'acceptanceDateTime', 'form', 'items', 'primaryDocument')
    if not all(isinstance(recent.get(k), list) for k in keys) or len({len(recent[k]) for k in keys}) != 1:
        raise ValueError('candidate filing schema unavailable')
    eligible = []
    for values in zip(*(recent[k] for k in keys)):
        row = dict(zip(keys, values))
        if row['form'] not in {'8-K', '8-K/A'}:
            continue
        published = timestamp(row['acceptanceDateTime'])
        if at - timedelta(days=lookback_days) <= published <= at:
            eligible.append((published, row))
    result = {'coverage': 'SEC_FILINGS_ONLY', 'candidates': [], 'status': 'AVAILABLE',
              'matched_filings': len(eligible), 'truncated': len(eligible) > limit}
    for published, row in sorted(eligible, key=lambda v: (v[0], v[1]['accessionNumber']), reverse=True)[:limit]:
        try:
            observed = sec.document(cik, row['accessionNumber'], row['primaryDocument'])
            body_hash = hashlib.sha256(observed['body'].encode()).hexdigest()
            item = dict(symbol=symbol, security_id=security_id, cik=cik, accession=row['accessionNumber'],
                        form=row['form'], items=row['items'], source_url=observed['source_url'],
                        published_at=published.isoformat(), received_at=observed['received_at'],
                        suggested_entry_session=entry_session(published).isoformat(), body=observed['body'],
                        body_sha256=body_hash, publication_basis='SEC filing acceptance; not original release time')
            item['document_id'] = document_id(item)
            result['candidates'].append(item)
        except Exception:
            result.update(status='UNAVAILABLE', reason='SEC document retrieval failed; stopped without retries')
            break
    return result


def reviewed_candidates(candidates, reviews, at):
    accepted, outcomes = [], []
    by_id = {}
    for value in reviews:
        review = CatalystReview.model_validate(value)
        if review.document_id in by_id:
            raise ValueError('duplicate catalyst review')
        by_id[review.document_id] = review
    for item in candidates:
        identity = item['document_id']
        if document_id(item) != identity or hashlib.sha256(item['body'].encode()).hexdigest() != item['body_sha256']:
            raise ValueError('catalyst content changed')
        review = by_id.get(identity)
        status = 'PENDING_REVIEW'
        if review is not None:
            if (review.symbol, review.security_id) != (item['symbol'], item['security_id']):
                raise ValueError('catalyst review identity mismatch')
            if not timestamp(item['published_at']) <= review.reviewed_at <= at <= review.valid_until:
                status = 'STALE_OR_FUTURE_REVIEW'
            elif review.decision == 'rejected':
                status = 'REJECTED'
            elif any(c['form'] == '8-K/A' and timestamp(c['published_at']) > timestamp(item['published_at'])
                     for c in candidates):
                status = 'REQUIRES_AMENDMENT_REVIEW'
            else:
                # Item 2.02 is an additional check, not proof of relevance/publication.
                items = set(re.split(r'[,;\s]+', item['items']))
                if review.kind == 'earnings' and ('2.02' not in items or review.report_period_end is None):
                    raise ValueError('earnings review requires results item and reported period')
                accepted.append((Catalyst(source_ref=item['source_url'], published_at=item['published_at'],
                    received_at=item['received_at'], entry_session=entry_session(timestamp(item['published_at'])),
                    kind=review.kind, summary=review.summary, relevance_ref=review.relevance_ref,
                    report_period_end=review.report_period_end), review.valid_until))
                status = 'ACCEPTED'
        outcomes.append({'document_id': identity, 'status': status})
    for identity in set(by_id) - {i['document_id'] for i in candidates}:
        outcomes.append({'document_id': identity, 'status': 'SOURCE_NOT_IN_CURRENT_COLLECTION'})
    return accepted, outcomes


def main(argv=None):
    """Probe original documents from a verified archive without refreshing paid feeds."""
    import argparse
    from getpass import getpass
    import os
    from pathlib import Path
    from desk.earnings_check import aware_time
    from desk.earnings_normalize import Archive
    from desk.earnings_sources import save
    from desk.sec import SecData
    parser = argparse.ArgumentParser(description='Read-only original SEC filing check; no catalyst approval')
    parser.add_argument('--archive', required=True)
    parser.add_argument('--at', required=True, type=aware_time, help='historical archive cutoff; document receipts retain actual time')
    parser.add_argument('--symbol', required=True)
    parser.add_argument('--security-id', required=True)
    parser.add_argument('--cik', required=True)
    parser.add_argument('--lookback-days', type=int, default=90)
    parser.add_argument('--limit', type=int, default=1)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        if Path(args.output).exists():
            raise ValueError('output exists')
        archive = Archive(args.archive, args.at)
        symbol = canonical_symbol(args.symbol)
        sub = archive.get('sec', symbol, 'submissions')
        sec = SecData(os.environ.get('SEC_USER_AGENT') or getpass('SEC application/contact (hidden, not saved): '))
        result = collect_candidates(sec, sub, symbol=symbol, security_id=args.security_id, cik=args.cik,
                                    at=args.at, lookback_days=args.lookback_days, limit=args.limit)
        result.update(purpose='original SEC document access only; no catalyst approval or decision eligibility',
                      archive_cutoff=args.at.isoformat())
        save(Path(args.output), result)
        result = {**result, 'candidates': [{k:v for k,v in c.items() if k!='body'} for c in result['candidates']]}
        if result['status'] == 'AVAILABLE' and not result['candidates']:
            result['status'] = 'NO_DOCUMENT_TESTED'
    except Exception:
        result = {'status': 'UNAVAILABLE', 'reason': 'archive, source or output unavailable; no retry'}
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'AVAILABLE' else 1


if __name__ == '__main__':
    raise SystemExit(main())
