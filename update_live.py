"""
Live front-month price updater — writes live.json for the dashboard ticker.

Lightweight companion to update_data.py. The dashboard polls live.json to update
the gold ticker box + sparkline + dot in place.

PRICE SOURCE (front-month ticker + sparkline), in priority order:
  * Yahoo Finance (CL=F / NG=F) — PRIMARY. A 2026-06-24 live-market test found
    Yahoo's quote consistently ~10 min old vs OilPriceAPI's ~15-20 min, so despite
    Yahoo's nominal exchange delay it is in practice the fresher source, with no key
    or rate limit. Self-contained (its own prev-close + intraday spark).
  * OilPriceAPI (https://oilpriceapi.com) — FALLBACK if OILPRICE_API_KEY is set and
    Yahoo errors. WTI_USD / NATURAL_GAS_USD, email-key signup (no KYC). Free tier is
    ~20 requests/hour, so we spend exactly ONE call per commodity per cycle, throttle
    to ~7 min between pulls, and carry prev-close + accumulate the sparkline locally.
  * OANDA v20 (real-time CFD) — FALLBACK if an OANDA token is configured. WTICO_USD /
    NATGAS_USD; requires demo-account KYC. Last-resort before the ticker goes blank.

The FORWARD CURVE overlay (live_curve.json) stays on Yahoo dated contracts — OANDA
only quotes the front CFD, not the 36-month strip, and the curve barely moves
intraday so the delay is immaterial there.
"""
import os
import json
import requests
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

DIR = Path(__file__).parent
OUT = DIR / 'live.json'
CURVE_OUT = DIR / 'live_curve.json'         # intraday forward-curve overlay
ET  = ZoneInfo('America/New_York')          # NYMEX exchange timezone

SYMBOLS = {'wti': 'CL=F', 'hh': 'NG=F'}
UA = {'User-Agent': 'Mozilla/5.0'}


# ── OANDA real-time config ────────────────────────────────────────────────────
def _secret(env_name: str, fname: str) -> str:
    """Read a secret from an env var (CI) or a git-ignored local file (manual run)."""
    v = os.environ.get(env_name, '').strip()
    if v:
        return v
    p = DIR / fname
    return p.read_text(encoding='utf-8').strip() if p.exists() else ''

OANDA_TOKEN   = _secret('OANDA_TOKEN', 'oanda_token.txt')
OANDA_ACCOUNT = _secret('OANDA_ACCOUNT', 'oanda_account.txt')   # optional — auto-resolved if blank
# Personal-access tokens from a *demo* (practice) account use the practice host;
# a funded live account uses api-fxtrade. Default to practice (free).
OANDA_HOST = ('https://api-fxtrade.oanda.com'
              if os.environ.get('OANDA_ENV', 'practice').lower() == 'live'
              else 'https://api-fxpractice.oanda.com')
OANDA_INSTR = {'wti': 'WTICO_USD', 'hh': 'NATGAS_USD'}

# ── OilPriceAPI real-time config (preferred front-month source) ────────────────
OILPRICE_KEY  = _secret('OILPRICE_API_KEY', 'oilprice_key.txt')
OILPRICE_HOST = 'https://api.oilpriceapi.com'
OILPRICE_CODE = {'wti': 'WTI_USD', 'hh': 'NATURAL_GAS_USD'}
# Free tier ≈ 20 req/hr. We make 1 call per commodity per pull, so pull at most
# every OILPRICE_MIN_MIN minutes (≈17 calls/hr for 2 commodities) — comfortably
# under the cap even with a 5-min external cron poking the workflow.
OILPRICE_MIN_MIN = 7
SPARK_MAX        = 160   # cap the locally-accumulated sparkline length

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


# ── OANDA real-time front-month (CFD) ─────────────────────────────────────────
_OANDA_ACCT_CACHE = None


def _oanda_headers() -> dict:
    return {'Authorization': f'Bearer {OANDA_TOKEN}'}


def oanda_account_id() -> str:
    """Resolve the account id from the token (so only OANDA_TOKEN must be set)."""
    global _OANDA_ACCT_CACHE
    if OANDA_ACCOUNT:
        return OANDA_ACCOUNT
    if _OANDA_ACCT_CACHE:
        return _OANDA_ACCT_CACHE
    r = requests.get(f'{OANDA_HOST}/v3/accounts', headers=_oanda_headers(), timeout=20)
    r.raise_for_status()
    _OANDA_ACCT_CACHE = r.json()['accounts'][0]['id']
    return _OANDA_ACCT_CACHE


def _norm_time(s: str) -> str:
    """OANDA RFC3339 (up to 9 fractional digits + 'Z') -> JS-parseable ISO (UTC)."""
    s = s.rstrip('Z').split('.')[0]
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).isoformat()


def _oanda_candles(instrument: str, **params) -> list:
    url = f'{OANDA_HOST}/v3/instruments/{instrument}/candles'
    r = requests.get(url, params={'price': 'M', **params}, headers=_oanda_headers(), timeout=20)
    r.raise_for_status()
    return r.json().get('candles', [])


def oanda_quote(key: str) -> dict:
    """Real-time CFD quote + intraday spark for one commodity, from OANDA."""
    instrument = OANDA_INSTR[key]
    acct = oanda_account_id()

    # Current price (mid of bid/ask).
    r = requests.get(f'{OANDA_HOST}/v3/accounts/{acct}/pricing',
                     params={'instruments': instrument}, headers=_oanda_headers(), timeout=20)
    r.raise_for_status()
    p     = r.json()['prices'][0]
    bid   = float(p['bids'][0]['price'])
    ask   = float(p['asks'][0]['price'])
    price = (bid + ask) / 2

    # Previous trading day's close = last *completed* daily candle.
    daily = [c for c in _oanda_candles(instrument, granularity='D', count=3) if c.get('complete')]
    prev_close = float(daily[-1]['mid']['c']) if daily else price

    # Today's intraday 5-min closes for the sparkline (from start of the ET day).
    et_midnight = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    frm = et_midnight.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    spark = [round(float(c['mid']['c']), 4)
             for c in _oanda_candles(instrument, granularity='M5', **{'from': frm})
             if c.get('complete')]

    change = price - prev_close
    pct    = (change / prev_close * 100) if prev_close else 0.0
    return {
        'price':       round(price, 4),
        'prev_close':  round(prev_close, 4),
        'change':      round(change, 4),
        'pct':         round(pct, 2),
        'market_time': _norm_time(p['time']),
        'spark':       spark,
        'source':      'oanda',
    }


def fetch_yahoo(key: str, sym: str) -> dict:
    """Delayed Yahoo fallback (front-month future + intraday spark)."""
    q = fetch_quote(sym)
    try:
        q['spark'] = fetch_intraday(sym)
    except Exception as se:
        q['spark'] = []
        print(f"{key.upper():3} {sym}: spark error {se}")
    q['source'] = 'yahoo'
    return q


# ── OilPriceAPI real-time front-month ─────────────────────────────────────────
def _read_prev_live() -> dict:
    """Previous live.json — carries prev_close + the accumulated spark between runs."""
    if OUT.exists():
        try:
            return json.loads(OUT.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}


def oilprice_due(prev: dict) -> bool:
    """True if >= OILPRICE_MIN_MIN has passed since the last *actual* OilPriceAPI pull.

    Gated on a dedicated `oilprice_pulled_at` stamp — NOT generated_at, which every
    run rewrites (that would throttle forever after the first pull). Absent stamp
    (cold start / source just switched on) -> due, so the first pull always fires.
    """
    ts = (prev or {}).get('oilprice_pulled_at')
    if not ts:
        return True
    try:
        age = (datetime.now().astimezone() - datetime.fromisoformat(ts)).total_seconds() / 60
        return age >= OILPRICE_MIN_MIN
    except Exception:
        return True


def oilprice_quote(key: str, prev: dict) -> dict:
    """Live price from OilPriceAPI (1 call); prev_close + spark kept locally.

    Free tier is tight, so this spends exactly one request (the latest price). The
    previous close is carried forward from the prior run (same source, no extra
    call); the sparkline is built by appending each poll and resets at the ET day
    rollover — keeping the whole front-month pull at 1 API call per commodity.
    """
    code = OILPRICE_CODE[key]
    r = requests.get(f'{OILPRICE_HOST}/v1/prices/latest', params={'by_code': code},
                     headers={'Authorization': f'Token {OILPRICE_KEY}'}, timeout=20)
    r.raise_for_status()
    d = r.json()
    d = d.get('data', d)                      # API wraps the payload in "data"
    price = float(d['price'])
    created = d.get('created_at') or d.get('created')
    market_time = created or datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    today = datetime.now(ET).date().isoformat()

    # Carry prev_close + spark from the prior run; reset both at a new trading day.
    prior = (prev or {}).get(key) or {}
    is_oilprice = prior.get('source') == 'oilprice'
    if is_oilprice and prior.get('day') == today:
        prev_close = float(prior.get('prev_close', price))
        spark = list(prior.get('spark', []))
    else:
        # New day -> yesterday's last observed price becomes today's reference close.
        prev_close = float(prior.get('price', price)) if is_oilprice else price
        spark = []
    spark.append(round(price, 4))
    spark = spark[-SPARK_MAX:]

    change = price - prev_close
    pct    = (change / prev_close * 100) if prev_close else 0.0
    return {
        'price':       round(price, 4),
        'prev_close':  round(prev_close, 4),
        'change':      round(change, 4),
        'pct':         round(pct, 2),
        'market_time': market_time,
        'spark':       spark,
        'day':         today,
        'source':      'oilprice',
    }


def main() -> None:
    fallbacks = ', '.join(f for f, on in
                          (('OilPriceAPI', OILPRICE_KEY), ('OANDA', OANDA_TOKEN)) if on)
    print(f"Price source: Yahoo (primary)"
          + (f"; fallbacks: {fallbacks}" if fallbacks else ""))

    prev = _read_prev_live()
    now_iso = datetime.now().astimezone().isoformat()
    # OilPriceAPI is now only a fallback (on Yahoo failure); its throttle still
    # gates the rare pull so we never blow the free-tier budget.
    fetch_oilprice = bool(OILPRICE_KEY) and oilprice_due(prev)

    out = {'generated_at': now_iso}
    pulled_ok = False
    for key, sym in SYMBOLS.items():
        q = None
        # PRIMARY: Yahoo (per 2026-06-24 test, fresher in practice than OilPriceAPI).
        try:
            q = fetch_yahoo(key, sym)
        except Exception as e:
            print(f"{key.upper():3} Yahoo error: {e} — trying fallbacks")
        # FALLBACK 1: OilPriceAPI (only if Yahoo failed).
        if q is None and OILPRICE_KEY:
            if fetch_oilprice:
                try:
                    q = oilprice_quote(key, prev)
                    pulled_ok = True
                except Exception as e:
                    print(f"{key.upper():3} OilPriceAPI error: {e} — falling back")
            elif (prev.get(key) or {}).get('source') == 'oilprice':
                q = prev[key]                 # within throttle window: reuse last pull
        # FALLBACK 2: OANDA (only if both above failed).
        if q is None and OANDA_TOKEN:
            try:
                q = oanda_quote(key)
            except Exception as e:
                print(f"{key.upper():3} OANDA error: {e}")
        out[key] = q
        if q:
            print(f"{key.upper():3} {q['source']:5}: {q['price']}  "
                  f"({q['change']:+} / {q['pct']:+}%)  {len(q['spark'])} pts  "
                  f"as of {q['market_time']}")

    # Throttle clock advances only on a *successful* pull; a failed/throttled run
    # keeps the prior stamp so the next run is due to retry rather than blocked.
    if OILPRICE_KEY:
        out['oilprice_pulled_at'] = now_iso if pulled_ok else prev.get('oilprice_pulled_at')

    OUT.write_text(json.dumps(out, indent=2), encoding='utf-8')
    print(f"Written -> {OUT}")

    # Forward-curve overlay (throttled internally so it doesn't run every cycle).
    try:
        update_live_curve()
    except Exception as e:
        print(f"Live curve update failed: {e}")


if __name__ == '__main__':
    main()
