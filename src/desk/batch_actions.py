"""Opt-in daily US corporate-action observations, not a complete action ledger.

Massive free Stocks plan: two-year history, daily updates, 5 requests/minute.
Query market-wide splits and current-session dividends, not one call per ticker.
Never use this provider's adjusted bars/volume or cumulative adjustment factors.
Plan B: unavailable volume; unexplained price mismatch stays unavailable.
"""
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib import error, parse, request

from desk.bars import BarDataError
from desk.calendar import clock
from desk.data_basis import VolumeBasis, AnchorAdjustment

HOST = 'api.massive.com'
REF = 'https://massive.com/docs/rest/stocks/corporate-actions/'
POLICY = 'webull-native-no-split-window-v1'


class BatchActionError(BarDataError):
    pass


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _fetch(req):
    with request.build_opener(_NoRedirect()).open(req, timeout=15) as response:
        return response.read()


def _number(value):
    if isinstance(value, bool):
        raise BatchActionError('ACTION_INVALID_NUMBER')
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise BatchActionError('ACTION_INVALID_NUMBER') from None
    if not number.is_finite() or number < 0:
        raise BatchActionError('ACTION_INVALID_NUMBER')
    return number


class BatchActions:
    def __init__(self, path, api_key, *, clock_fn, transport=_fetch, volume_policy=None):
        if not isinstance(api_key,str) or not api_key.strip() or volume_policy not in {None, POLICY}:
            raise BatchActionError('AUTO_ACTION_CONFIGURATION_INVALID')
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._key, self._clock, self._transport = api_key.strip(), clock_fn, transport
        self.volume_policy = volume_policy
        with closing(self._db()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS feeds(kind TEXT PRIMARY KEY, day TEXT,
                    received TEXT, payload TEXT, status TEXT);
                CREATE TABLE IF NOT EXISTS calls(at TEXT);
                CREATE TABLE IF NOT EXISTS limits(day TEXT PRIMARY KEY);
            ''')

    def _db(self):
        # Explicit closure is supplied by callers; sqlite context owns commit only.
        return sqlite3.connect(self.path, timeout=10)

    def _reserve(self, now):
        with closing(self._db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM limits WHERE day=?',(str(clock(now).date()),)).fetchone():
                raise BatchActionError('ACTION_RATE_LIMITED')
            cutoff = (clock(now)-timedelta(seconds=60)).isoformat()
            db.execute('DELETE FROM calls WHERE at<=?',(cutoff,))
            if db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] >= 5:
                raise BatchActionError('ACTION_REQUEST_BUDGET_EXHAUSTED')
            db.execute('INSERT INTO calls VALUES (?)',(clock(now).isoformat(),))

    def _safe_url(self, url, kind):
        try:
            part = parse.urlsplit(url)
        except ValueError:
            raise BatchActionError("ACTION_UNSAFE_PAGINATION") from None
        if (part.scheme != 'https' or part.netloc != HOST or part.path != '/stocks/v1/'+kind
                or part.fragment or part.username or part.password):
            raise BatchActionError('ACTION_UNSAFE_PAGINATION')
        args = parse.parse_qsl(part.query,keep_blank_values=True)
        # Only this key is sent; provider-supplied credential parameters are dropped.
        args = [(k,v) for k,v in args if k.lower() != 'apikey']
        return parse.urlunsplit(('https',HOST,part.path,parse.urlencode(args),'')).rstrip('?')

    def _page(self, url, kind):
        safe = self._safe_url(url,kind)
        self._reserve(self._clock())
        req = request.Request(safe,headers={'Authorization':'Bearer '+self._key,'Accept':'application/json'})
        try:
            raw = self._transport(req)
        except error.HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 429:
                with closing(self._db()) as db, db:
                    db.execute('INSERT OR IGNORE INTO limits VALUES (?)',(str(clock(self._clock()).date()),))
            raise BatchActionError('ACTION_RATE_LIMITED' if status == 429 else 'ACTION_HTTP_FAILURE') from None
        except (OSError, TimeoutError):
            raise BatchActionError('ACTION_TRANSPORT_FAILURE') from None
        try:
            body = json.loads(raw,parse_float=Decimal)
        except (ValueError,TypeError):
            raise BatchActionError('ACTION_INVALID_JSON') from None
        if self._key in json.dumps(body,default=str):
            raise BatchActionError('ACTION_UNSAFE_RESPONSE')
        if not isinstance(body,dict) or body.get('status') != 'OK' or not isinstance(body.get('results'),list) or any(k in body for k in ('error','Error Message','Information','Note')):
            raise BatchActionError('ACTION_INVALID_ENVELOPE')
        return body

    def _rows(self, rows, kind, first, last):
        output = []
        for row in rows:
            if not isinstance(row,dict) or not isinstance(row.get('ticker'),str) or not re.fullmatch(r'[A-Z0-9][A-Z0-9.-]{0,19}',row['ticker']):
                raise BatchActionError('ACTION_INVALID_IDENTITY')
            key = 'execution_date' if kind == 'splits' else 'ex_dividend_date'
            try:
                day = date.fromisoformat(row[key])
            except (KeyError,ValueError,TypeError):
                raise BatchActionError('ACTION_INVALID_DATE') from None
            if not first <= day <= last or not isinstance(row.get('id'),str) or not row['id']:
                raise BatchActionError('ACTION_OUTSIDE_QUERY_OR_MISSING_ID')
            out = {'symbol':row['ticker'],'day':str(day),'id':row['id']}
            if kind == 'splits':
                old,new = _number(row.get('split_from')),_number(row.get('split_to'))
                if old <= 0 or new <= 0 or old == new or row.get('adjustment_type') not in {'forward_split','reverse_split','stock_dividend'}:
                    raise BatchActionError('ACTION_UNSUPPORTED_SPLIT')
                if row['adjustment_type'] == 'forward_split' and new <= old or row['adjustment_type'] == 'reverse_split' and new >= old:
                    raise BatchActionError('ACTION_CONTRADICTORY_SPLIT')
                out.update(kind=row['adjustment_type'],ratio=str(new/old))
            else:
                amount = _number(row.get('cash_amount'))
                if not isinstance(row.get('currency'),str):
                    raise BatchActionError('ACTION_MISSING_CURRENCY')
                out.update(kind='cash_dividend',amount=str(amount),currency=row['currency'])
            output.append(out)
        return output

    def snapshot(self, kind, *, after=None):
        if kind not in {'splits','dividends'}:
            raise BatchActionError('ACTION_UNKNOWN_FEED')
        now = self._clock(); day = clock(now).date()
        if after is not None and clock(after) > clock(now):
            raise BatchActionError('ACTION_REFRESH_CLOCK_IN_FUTURE')
        first = day-timedelta(days=730) if kind == 'splits' else day
        with closing(self._db()) as db:
            prior = db.execute('SELECT day,received,payload,status FROM feeds WHERE kind=?',(kind,)).fetchone()
        if prior and prior[1] is not None and clock(prior[1]) > clock(now):
            raise BatchActionError('ACTION_CLOCK_MOVED_BACKWARDS')
        if prior and prior[3] == 'READY' and prior[0] == str(day) and clock(prior[1]) <= clock(now) and (after is None or clock(prior[1]) >= clock(after)):
            return json.loads(prior[2])
        field = 'execution_date' if kind == 'splits' else 'ex_dividend_date'
        url = 'https://'+HOST+'/stocks/v1/'+kind+'?'+parse.urlencode({field+'.gte':str(first),field+'.lte':str(day),'limit':5000,'sort':field+'.asc'})
        rows,seen,issues = [],set(),{}
        try:
            # Bounded pagination: never publish a truncated all-market response.
            for _ in range(4):
                safe = self._safe_url(url,kind)
                if safe in seen:
                    raise BatchActionError('ACTION_PAGINATION_LOOP')
                seen.add(safe)
                body = self._page(safe,kind)
                for raw_row in body['results']:
                    try:
                        rows += self._rows([raw_row],kind,first,day)
                    except BatchActionError as exc:
                        symbol = raw_row.get('ticker') if isinstance(raw_row,dict) else None
                        if not isinstance(symbol,str) or not symbol:
                            raise BatchActionError('ACTION_UNATTRIBUTABLE_ROW') from None
                        issues[symbol] = str(exc)
                following = body.get('next_url')
                if following is None:
                    break
                if not isinstance(following,str) or not following:
                    raise BatchActionError('ACTION_INVALID_PAGINATION')
                url = following
            else:
                raise BatchActionError('ACTION_INCOMPLETE_PAGINATION')
            ids = {}
            for row in rows:
                if row['id'] in ids:
                    issues[row['symbol']] = issues[ids[row['id']]] = 'ACTION_DUPLICATE_ID'
                ids[row['id']] = row['symbol']
            received = self._clock()
            if clock(received).date() != day or clock(received) < clock(now):
                raise BatchActionError('ACTION_RECEIPT_CLOCK_INVALID')
            content = {'first':str(first),'last':str(day),'rows':rows,'issues':issues}
            digest = hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest()
            result = {**content,'received_at':clock(received).isoformat(),'digest':digest,'source':REF+kind}
            with closing(self._db()) as db, db:
                db.execute('INSERT OR REPLACE INTO feeds VALUES (?,?,?,?,?)',(kind,str(day),result['received_at'],json.dumps(result),'READY'))
            return result
        except BatchActionError:
            with closing(self._db()) as db, db:
                db.execute('INSERT OR REPLACE INTO feeds VALUES (?,?,?,?,?)',(kind,str(day),None,None,'UNAVAILABLE'))
            raise

    def volume(self, metadata, timeframe, *, after=None):
        if self.volume_policy != POLICY:
            raise BatchActionError('NATIVE_VOLUME_POLICY_NOT_ACCEPTED')
        snap = self.snapshot('splits',after=after)
        if metadata.symbol in snap['issues']:
            raise BatchActionError(snap['issues'][metadata.symbol])
        splits = [r for r in snap['rows'] if r['symbol'] == metadata.symbol]
        start = max([snap['first']]+[r['day'] for r in splits])
        events = sorted((r['id'],r['day'],r['ratio']) for r in splits)
        share = hashlib.sha256(json.dumps([metadata.instrument_id,snap['first'],events],sort_keys=True).encode()).hexdigest()
        if timeframe not in {'D','M15'}:
            raise BatchActionError('ACTION_UNSUPPORTED_VOLUME_CHANNEL')
        channel = 'webull:native:D' if timeframe == 'D' else 'webull:minute:RTH'
        return VolumeBasis(source='Webull native shares; Massive reported split window',evidence_ref=snap['source']+'; snapshot:'+snap['digest'],
            channel=channel,definition_id=channel+':provider-reported',units='shares',share_basis_id=share,
            symbol=metadata.symbol,security_id=metadata.instrument_id,valid_from=start,valid_through=snap['last'],comparison_policy='webull-rth30/native-daily50-v1')

    def anchor(self, metadata, anchor_day):
        now = self._clock()
        if anchor_day >= clock(now).date():
            raise BatchActionError('NO_POST_ANCHOR_ACTION_INTERVAL')
        # An observed price conflict may precede the provider's daily update.
        # Refresh the shared batches at most once per 15-minute scan interval;
        # this is a retry cadence, not a claim about source latency.
        boundary = clock(now)-timedelta(minutes=15)
        splits = self.snapshot('splits',after=boundary)
        dividends = self.snapshot('dividends',after=boundary)
        if any(metadata.symbol in s['issues'] for s in (splits,dividends)):
            raise BatchActionError('ACTION_TICKER_EVIDENCE_INVALID')
        rows = [r for snap in (splits,dividends) for r in snap['rows'] if r['symbol'] == metadata.symbol and str(anchor_day) < r['day'] <= str(clock(now).date())
                and (r['kind'] != 'cash_dividend' or Decimal(r['amount']) != 0)]
        if len(rows) != 1 or rows[0]['day'] != str(clock(now).date()):
            raise BatchActionError('ACTION_MISSING_OR_AMBIGUOUS_ANCHOR')
        row = rows[0]
        if row['kind'] == 'cash_dividend' and row['currency'] != metadata.currency:
            raise BatchActionError('ACTION_CURRENCY_MISMATCH')
        split = row['kind'] != 'cash_dividend'
        snap = splits if split else dividends
        return AnchorAdjustment(source='Massive provider-reported action',evidence_ref=snap['source']+'; snapshot:'+snap['digest'],
            event_id=row['id'],effective_session=row['day'],verified_at=snap['received_at'],kind='split' if split else 'cash_dividend',
            value=row['ratio'] if split else row['amount'])
