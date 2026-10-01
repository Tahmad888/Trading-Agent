"""Read-only Step 06 acceptance on the credentialed host; no scanner run/orders.

Use the explicit ledger/channel files produced from reviewed, dated observations.
This command never calls Alpha Vantage, changes config, or activates a runner.
"""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path

from desk.action_source import configured_source
from desk.bar_contract import completed_daily, completed_intraday, check_price_scale
from desk.bars import BarDataError
from desk.calendar import clock, session, trading_day, latest_closed_session
from desk.data_basis import compatible_volume, price_basis, volume_basis
from desk.indicators import daily_features
from desk.webull import WebullData


# This diagnostic is explicitly bounded to the reviewed Step 06 identities.
ACCEPTANCE_CATEGORIES = {"NVDA": "US_STOCK", "SPY": "US_ETF"}

def check(source, symbols, *, clock_fn=lambda: datetime.now(timezone.utc)):
    result = {"purpose": "read-only integrated data acceptance; no signal/order activation", "checks": []}
    for symbol in symbols:
        item = {"symbol": symbol, "stages": {}}
        stage = "fetch_daily"
        try:
            if symbol not in ACCEPTANCE_CATEGORIES:
                raise BarDataError("Symbol outside reviewed acceptance scope")
            daily = source.bars([symbol], category=ACCEPTANCE_CATEGORIES[symbol], timespan="D", count=1000)[symbol]
            stage = "completed_daily_and_price_basis"
            now = clock_fn()
            d = completed_daily(daily, now)
            if len(d) < 260:
                raise BarDataError("Fewer than 260 completed warm-up sessions")
            item['stages'][stage] = 'PASS'
            item['daily_rows'] = len(d)
            item['latest_daily_session'] = str(d.index[-1].tz_convert('America/New_York').date())
            stage = "native_daily_volume_50"
            volume_basis(d.iloc[-50:])
            average = float(d.volume.iloc[-50:].mean())
            if average <= 0:
                raise BarDataError("Nonpositive 50-day daily-volume baseline")
            item['stages'][stage] = 'PASS'
            stage = 'price_indicators'
            features = daily_features(d)
            wanted = ['sma_200', 'adr_pct_20', 'rel_volume']
            if any(name not in features or not math.isfinite(features[name].iloc[-1]) for name in wanted):
                raise BarDataError("Required indicator is missing or not warmed up")
            item['stages'][stage] = 'PASS'
            item['indicators'] = {k: float(features[k].iloc[-1]) for k in wanted}
            stage = 'fetch_regular_minutes'
            now = clock_fn(); stamp = clock(now)
            # Outside RTH use an explicit completed historical session, no freshness claim.
            in_session = trading_day(stamp.date()) and session(stamp.date())[0] <= stamp < session(stamp.date())[1]
            day = stamp.date() if in_session else latest_closed_session(now)
            opened, closed = session(day)
            m = source.bars([symbol], category=ACCEPTANCE_CATEGORIES[symbol], timespan='M15', count=40,
                sessions='RTH', start_time=int(opened.timestamp()*1000),
                end_time=int((closed.timestamp()-1)*1000))[symbol]
            stage = 'completed_minutes_and_price_compatibility'
            now = clock_fn()
            m = completed_intraday(m, now, session_day=day)
            if len(m) < 2:
                raise BarDataError("Two completed opening bars are not yet available")
            check_price_scale(price_basis(d, now, symbol=symbol).model_dump(mode='json'), m, now, symbol=symbol)
            item['stages'][stage] = 'PASS'
            stage = 'ep_volume_source_pair'
            volume_basis(m.iloc[:2])
            # A historical session's EP baseline must exclude that session's daily bar.
            prior = d[d.index.tz_convert('America/New_York').date < day].iloc[-50:]
            if len(prior) != 50:
                raise BarDataError("Fewer than 50 previous daily volumes")
            compatible_volume(prior, m.attrs.get('volume_basis'))
            baseline = float(prior.volume.mean())
            if baseline <= 0:
                raise BarDataError("Nonpositive EP daily-volume baseline")
            item['stages'][stage] = 'PASS'
            item.update(status='PASS', checked_at=now.isoformat(), minute_session=str(day),
                timing_scope='current regular session' if in_session else 'completed historical session',
                ep_first30_volume=float(m.volume.iloc[:2].sum()), ep_prior50_daily_average=baseline,
                ep_fraction_of_daily_volume=float(m.volume.iloc[:2].sum()) / baseline,
                ep_threshold=0.5, setup_eligibility='NOT_EVALUATED')
        except BarDataError as exc:
            # Contract failures contain fixed local messages; transport errors may contain URLs.
            item['stages'][stage] = 'FAIL'
            item.update(status='FAIL', reason='DATA_CONTRACT_REJECTED', error_type=type(exc).__name__)
            if stage not in ('fetch_daily', 'fetch_regular_minutes') or type(exc) is BarDataError:
                item['detail'] = str(exc)
        result['checks'].append(item)
        if item['status'] == 'FAIL' and stage in ('fetch_daily', 'fetch_regular_minutes'):
            result['stopped_on_fetch_failure'] = True
            break  # no repeated authentication/rate-limit probes
    result['status'] = 'PASS' if len(result['checks']) == len(symbols) and all(c['status']=='PASS' for c in result['checks']) else 'FAIL'
    return result


def main(argv=None):
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--channels', required=True)
    parser.add_argument('--symbols', nargs='+', choices=['NVDA', 'SPY'], default=['NVDA', 'SPY'])
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    try:
        env = {**os.environ, 'DESK_ACTION_LEDGER': args.database, 'DESK_ACTION_CHANNELS': args.channels}
        source = configured_source(WebullData.from_env(), env)
        result = check(source, list(dict.fromkeys(args.symbols)))
    except (BarDataError, OSError, ValueError):
        result = {'status':'FAIL', 'reason':'CONFIGURATION_UNAVAILABLE'}
    rendered = json.dumps(result, indent=2)
    if args.output:
        Path(args.output).write_text(rendered+'\n')
    print(rendered)
    return 0 if result['status']=='PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
