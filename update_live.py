"""
Live front-month price updater — writes live.json for the dashboard ticker.

Lightweight companion to update_data.py. Hits Yahoo Finance's public chart
endpoint *server-side* (no API key, and no browser-CORS limits), so it can run
frequently during market hours via GitHub Actions. The dashboard polls the
resulting live.json to update the copper ticker box + dot in place.

  CL=F = WTI crude front-month continuous
  NG=F = Henry Hub natural gas front-month continuous
"""
import json
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

DIR = Path(__file__).parent
OUT = DIR / 'live.json'
ET  = ZoneInfo('America/New_York')          # NYMEX exchange timezone

SYMBOLS = {'wti': 'CL=F', 'hh': 'NG=F'}
UA = {'User-Agent': 'Mozilla/5.0'}


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


def main() -> None:
    out = {'generated_at': datetime.now().astimezone().isoformat()}
    for key, sym in SYMBOLS.items():
        try:
            q = fetch_quote(sym)
            try:
                q['spark'] = fetch_intraday(sym)
            except Exception as se:
                q['spark'] = []
                print(f"{key.upper():3} {sym}: spark error {se}")
            out[key] = q
            print(f"{key.upper():3} {sym}: {q['price']}  "
                  f"({q['change']:+} / {q['pct']:+}%)  {len(q['spark'])} pts  "
                  f"as of {q['market_time']}")
        except Exception as e:
            print(f"{key.upper():3} {sym}: error {e}")
            out[key] = None
    OUT.write_text(json.dumps(out, indent=2), encoding='utf-8')
    print(f"Written -> {OUT}")


if __name__ == '__main__':
    main()
