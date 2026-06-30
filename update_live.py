"""
Live front-month price updater — writes live.json for the dashboard ticker.

Lightweight companion to update_data.py. The dashboard polls live.json to update
the gold ticker box + sparkline + dot in place.

PRICE SOURCE: Yahoo Finance (CL=F / NG=F) for the front-month ticker + sparkline.
A 2026-06-24 live-market test found Yahoo's quote consistently ~10 min old vs the
OilPriceAPI/OANDA alternatives' ~15-20 min, so despite Yahoo's nominal exchange
delay it was in practice the fresher source — with no key or rate limit. Those
alternatives were removed 2026-06-30; Yahoo is now the sole source.

The FORWARD CURVE overlay (live_curve.json) also comes from Yahoo, using the dated
.NYM contract symbols.
"""
import json
import requests
from datetime import date, datetime
from zoneinfo import ZoneInfo
from pathlib import Path

DIR = Path(__file__).parent
OUT = DIR / 'live.json'
CURVE_OUT = DIR / 'live_curve.json'         # intraday forward-curve overlay
ET  = ZoneInfo('America/New_York')          # NYMEX exchange timezone

SYMBOLS = {'wti': 'CL=F', 'hh': 'NG=F'}
UA = {'User-Agent': 'Mozilla/5.0'}

# Full-strip (live forward curve) config. The strip is ~36 dated contracts per
# commodity, so it's far heavier than the 2-call front-month pull — we throttle
# it to refresh at most every CURVE_REFRESH_MIN minutes (the strip doesn't whip
# around like the front month, so intra-day-but-not-every-5-min is plenty).
CURVE_PREFIX     = {'wti': 'CL', 'hh': 'NG'}
MONTHS_FORWARD   = 36
CURVE_REFRESH_MIN = 20
MONTH_CODES = {1: 'F', 2: 'G', 3: 'H', 4: 'J',  5: 'K',  6: 'M',
               7: 'N', 8: 'Q', 9: 'U', 10: 'V', 11: 'X', 12: 'Z'}


def fetch_quote(symbol: str) -> dict:
    """Current front-month price + change vs. the previous trading day's close."""
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}'
    r = requests.get(url, params={'range': '10d', 'interval': '1d'},
                     headers=UA, timeout=20)
    r.raise_for_status()
    res  = r.json()['chart']['result'][0]
    meta = res['meta']

    price  = float(meta['regularMarketPrice'])
    mkt_dt = datetime.fromtimestamp(int(meta['regularMarketTime']), ET)
    today  = mkt_dt.date()

    # Daily candles — the last bar is today's (its close == the live price while
    # the market is open), so the previous trading day's close is the reference.
    ts     = res.get('timestamp', []) or []
    closes = res['indicators']['quote'][0].get('close', []) or []
    bars   = [(datetime.fromtimestamp(int(t), ET).date(), c)
              for t, c in zip(ts, closes) if c is not None]

    prior = [c for d, c in bars if d < today]
    if prior:
        prev_close = float(prior[-1])
    elif len(bars) >= 2:
        prev_close = float(bars[-2][1])
    else:
        prev_close = price

    change = price - prev_close
    pct    = (change / prev_close * 100) if prev_close else 0.0

    return {
        'price':       round(price, 4),
        'prev_close':  round(prev_close, 4),
        'change':      round(change, 4),
        'pct':         round(pct, 2),
        'market_time': mkt_dt.isoformat(),   # includes ET offset
    }


def fetch_intraday(symbol: str) -> list:
    """Today's intraday close series — feeds the ticker sparkline/area chart.

    A flat list of prices (5-min bars, nulls dropped). The dashboard shades it
    green/red against prev_close, so no timestamps are needed — even spacing is
    fine for a thumbnail. Failure is non-fatal: the caller just drops the spark.
    """
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}'
    r = requests.get(url, params={'range': '1d', 'interval': '5m'},
                     headers=UA, timeout=20)
    r.raise_for_status()
    res    = r.json()['chart']['result'][0]
    closes = res['indicators']['quote'][0].get('close', []) or []
    return [round(float(c), 4) for c in closes if c is not None]


def fetch_front_month(key: str, sym: str) -> dict:
    """Front-month quote + intraday spark for one commodity, from Yahoo."""
    q = fetch_quote(sym)
    try:
        q['spark'] = fetch_intraday(sym)
    except Exception as se:
        q['spark'] = []
        print(f"{key.upper():3} {sym}: spark error {se}")
    q['source'] = 'yahoo'
    return q


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def contract_symbol(prefix: str, yr: int, mo: int) -> str:
    """e.g. ('CL', 2026, 7) -> 'CLN26.NYM' (same dated NYMEX symbol yfinance uses)."""
    return f"{prefix}{MONTH_CODES[mo]}{str(yr)[2:]}.NYM"


def fetch_contract_price(symbol: str):
    """Latest live price for one dated contract via Yahoo's v8 chart endpoint.

    query2 serves the dated .NYM symbols (query1 sometimes 404s on them), so try
    it first and fall back. Returns None on any failure (caller drops the point).
    """
    for host in ('query2', 'query1'):
        try:
            url = f'https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}'
            r = requests.get(url, params={'range': '1d', 'interval': '1d'},
                             headers=UA, timeout=20)
            if r.status_code != 200:
                continue
            meta = r.json()['chart']['result'][0]['meta']
            p = meta.get('regularMarketPrice')
            if p is not None:
                return float(p)
        except Exception:
            continue
    return None


def fetch_live_curve(prefix: str, months: int = MONTHS_FORWARD) -> list:
    """Today's live forward curve: front month -> N months out, dated contracts.

    Mirrors update_data.py's strip exactly (add_months(today, i) deliveries) so the
    overlay lines up on the same x-values as the daily settled snapshot.
    """
    today = datetime.now(ET).date()
    strip = []
    for i in range(1, months + 1):
        d = add_months(today, i)
        price = fetch_contract_price(contract_symbol(prefix, d.year, d.month))
        if price is not None:
            strip.append({'delivery': f"{d.year}-{d.month:02d}", 'price': round(price, 4)})
    return strip


def curve_is_fresh() -> bool:
    """True if live_curve.json was refreshed < CURVE_REFRESH_MIN minutes ago."""
    if not CURVE_OUT.exists():
        return False
    try:
        prev = json.loads(CURVE_OUT.read_text(encoding='utf-8'))
        ts = datetime.fromisoformat(prev['generated_at'])
        age_min = (datetime.now().astimezone() - ts).total_seconds() / 60
        return 0 <= age_min < CURVE_REFRESH_MIN
    except Exception:
        return False


def update_live_curve() -> None:
    """Refresh live_curve.json unless it's still fresh (throttle the heavy pull)."""
    if curve_is_fresh():
        print(f"Live curve still fresh (<{CURVE_REFRESH_MIN}m) — skipping strip refresh.")
        return
    # ET (market) date, not the runner's UTC date — otherwise after ~8pm ET the
    # overlay stamps tomorrow and the "Today" line jumps a day ahead.
    today_str = datetime.now(ET).date().isoformat()
    out = {'generated_at': datetime.now().astimezone().isoformat()}
    for key, prefix in CURVE_PREFIX.items():
        try:
            strip = fetch_live_curve(prefix)
            out[key] = {'date': today_str, 'data': strip}
            print(f"{key.upper():3} live curve: {len(strip)}/{MONTHS_FORWARD} contracts")
        except Exception as e:
            print(f"{key.upper():3} live curve error: {e}")
            out[key] = None
    CURVE_OUT.write_text(json.dumps(out, indent=2), encoding='utf-8')
    print(f"Written -> {CURVE_OUT}")


def main() -> None:
    print("Price source: Yahoo")
    out = {'generated_at': datetime.now().astimezone().isoformat()}
    for key, sym in SYMBOLS.items():
        try:
            q = fetch_front_month(key, sym)
        except Exception as e:
            print(f"{key.upper():3} Yahoo error: {e}")
            q = None
        out[key] = q
        if q:
            print(f"{key.upper():3} {q['source']:5}: {q['price']}  "
                  f"({q['change']:+} / {q['pct']:+}%)  {len(q['spark'])} pts  "
                  f"as of {q['market_time']}")

    OUT.write_text(json.dumps(out, indent=2), encoding='utf-8')
    print(f"Written -> {OUT}")

    # Forward-curve overlay (throttled internally so it doesn't run every cycle).
    try:
        update_live_curve()
    except Exception as e:
        print(f"Live curve update failed: {e}")


if __name__ == '__main__':
    main()
