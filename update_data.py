"""
Energy Dashboard — Data Updater
Run daily (e.g. via Windows Task Scheduler) to refresh energy_data.json.

Sources:
  Spot history : EIA API v2 (weekly WTI spot, daily HH spot)
  Futures strip: Yahoo Finance via yfinance (NYMEX delayed quotes)

Outputs:
  energy_data.json      — consumed by dashboard.html
  curve_snapshots.json  — accumulates strip snapshots over time
"""

import os
import json
import warnings
import requests
import pandas as pd
import yfinance as yf
from datetime import date, datetime, timedelta
from pathlib import Path

warnings.filterwarnings('ignore')

# ── Config ────────────────────────────────────────────────────────────────────
DIR            = Path(__file__).parent

# EIA key is never committed. The GitHub Action injects it via the EIA_KEY env
# var (from an encrypted repo secret). For local/manual runs, drop the key into
# eia_key.txt next to this script (git-ignored).
def _load_eia_key() -> str:
    key = os.environ.get('EIA_KEY', '').strip()
    if key:
        return key
    keyfile = DIR / 'eia_key.txt'
    if keyfile.exists():
        return keyfile.read_text(encoding='utf-8').strip()
    raise SystemExit("No EIA key found. Set the EIA_KEY env var or create eia_key.txt.")

EIA_KEY        = _load_eia_key()
DATA_FILE      = DIR / 'energy_data.json'
SNAP_FILE      = DIR / 'curve_snapshots.json'
MONTHS_HISTORY = 18
MONTHS_FORWARD = 36
BACKFILL_MONTHS = 36   # how many months ahead to look when reconstructing past curves

# CME/NYMEX futures month codes
MONTH_CODES = {1:'F',2:'G',3:'H',4:'J',5:'K',6:'M',
               7:'N',8:'Q',9:'U',10:'V',11:'X',12:'Z'}


# ── Helpers ───────────────────────────────────────────────────────────────────

def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def make_ticker(prefix: str, yr: int, mo: int, exch: str = '.NYM') -> str:
    return f"{prefix}{MONTH_CODES[mo]}{str(yr)[2:]}{exch}"


# ── EIA Spot History ──────────────────────────────────────────────────────────

def eia_spot(series: str, route: str, frequency: str = 'weekly') -> list[dict]:
    start = add_months(date.today(), -MONTHS_HISTORY).strftime('%Y-%m-%d')
    try:
        r = requests.get(
            f'https://api.eia.gov/v2/{route}/data/',
            params={
                'api_key':              EIA_KEY,
                'frequency':            frequency,
                'data[0]':              'value',
                'facets[series][]':     series,
                'start':                start,
                'sort[0][column]':      'period',
                'sort[0][direction]':   'asc',
                'length':               5000,
            },
            timeout=30,
        )
        r.raise_for_status()
        rows = r.json().get('response', {}).get('data', [])
        return [
            {'date': x['period'], 'price': round(float(x['value']), 2)}
            for x in rows if x.get('value') is not None
        ]
    except Exception as e:
        print(f"  EIA error ({series}): {e}")
        return []


# ── yfinance Futures ──────────────────────────────────────────────────────────

def _fetch_contract(ticker: str, **hist_kwargs) -> float | None:
    # Try auto_adjust=True first, then False — the latter handles expired contracts
    for adj in [True, False]:
        try:
            h = yf.Ticker(ticker).history(**{**hist_kwargs, 'auto_adjust': adj})
            if not h.empty and 'Close' in h.columns:
                closes = h['Close'].dropna()
                if not closes.empty:
                    return round(float(closes.iloc[-1]), 2)
        except Exception:
            continue
    return None


def fetch_strip(prefix: str, months: int = MONTHS_FORWARD) -> list[dict]:
    """Fetch today's forward curve (today → N months out)."""
    today = date.today()
    strip = []
    for i in range(1, months + 1):
        d = ticker_dt = add_months(today, i)
        t = make_ticker(prefix, d.year, d.month)
        price = _fetch_contract(t, period='5d')
        status = f"${price:.2f}" if price else "—"
        print(f"  {t}: {status}")
        if price is not None:
            strip.append({'delivery': f"{d.year}-{d.month:02d}", 'price': price})
    return strip


def fetch_historical_strip(prefix: str, snap_date: date, months: int = BACKFILL_MONTHS) -> list[dict]:
    """Reconstruct a historical forward curve for a past date.
    Uses batch yf.download(auto_adjust=False) first, which handles expired
    contracts more reliably than Ticker.history() in yfinance 1.4.x.
    """
    start = (snap_date - timedelta(days=5)).strftime('%Y-%m-%d')
    end   = (snap_date + timedelta(days=5)).strftime('%Y-%m-%d')

    tickers, dmap = [], {}
    for i in range(1, months + 1):
        d = add_months(snap_date, i)
        t = make_ticker(prefix, d.year, d.month)
        tickers.append(t)
        dmap[t] = f"{d.year}-{d.month:02d}"

    found: dict[str, float] = {}

    # Batch download — auto_adjust=False bypasses timezone metadata check on expired tickers
    try:
        raw = yf.download(tickers, start=start, end=end, progress=False, auto_adjust=False)
        if not raw.empty:
            if isinstance(raw.columns, pd.MultiIndex):
                if 'Close' in raw.columns.get_level_values(0):
                    for t in tickers:
                        try:
                            col = raw['Close'][t].dropna()
                            if not col.empty:
                                found[t] = round(float(col.iloc[-1]), 2)
                        except Exception:
                            pass
            elif 'Close' in raw.columns and len(tickers) == 1:
                col = raw['Close'].dropna()
                if not col.empty:
                    found[tickers[0]] = round(float(col.iloc[-1]), 2)
    except Exception as e:
        print(f"  Batch download error: {e}")

    # Individual fallback for any still-missing tickers
    for t in tickers:
        if t not in found:
            price = _fetch_contract(t, start=start, end=end)
            if price is not None:
                found[t] = price

    strip = [{'delivery': dmap[t], 'price': found[t]} for t in tickers if t in found]
    return sorted(strip, key=lambda x: x['delivery'])


# ── Snapshot Store ────────────────────────────────────────────────────────────

def load_snaps() -> dict:
    if SNAP_FILE.exists():
        return json.loads(SNAP_FILE.read_text(encoding='utf-8'))
    return {'wti': {}, 'hh': {}}


def save_snaps(s: dict) -> None:
    SNAP_FILE.write_text(json.dumps(s, indent=2), encoding='utf-8')


def nearest(snaps: dict, target: date, tol: int = 14) -> str | None:
    """Return the snapshot date key closest to target within tol days."""
    best, best_d = None, tol + 1
    for k in snaps:
        try:
            d = abs((datetime.strptime(k, '%Y-%m-%d').date() - target).days)
            if d < best_d:
                best, best_d = k, d
        except Exception:
            pass
    return best


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    today     = date.today()
    today_str = today.strftime('%Y-%m-%d')
    print(f"=== Energy Dashboard Update — {today_str} ===\n")

    snaps = load_snaps()

    # ── 1. EIA spot history ───────────────────────────────────────────────────
    print("Fetching EIA spot prices...")

    wti_spot = eia_spot('RWTC', 'petroleum/pri/spt', 'weekly')
    print(f"  WTI: {len(wti_spot)} weekly records")

    # HH spot lives in the natural-gas/pri/fut route (not pri/sum)
    hh_spot = eia_spot('RNGWHHD', 'natural-gas/pri/fut', 'weekly')
    print(f"  HH:  {len(hh_spot)} records")

    # ── 2. Current futures strip ──────────────────────────────────────────────
    print(f"\nFetching WTI futures strip (up to {MONTHS_FORWARD} months)...")
    wti_strip = fetch_strip('CL')
    print(f"  → {len(wti_strip)} contracts with data")

    print(f"\nFetching HH futures strip (up to {MONTHS_FORWARD} months)...")
    hh_strip = fetch_strip('NG')
    print(f"  → {len(hh_strip)} contracts with data")

    if wti_strip: snaps['wti'][today_str] = wti_strip
    if hh_strip:  snaps['hh'][today_str]  = hh_strip

    # ── 3. Backfill missing historical snapshots ──────────────────────────────
    targets = {
        '1d':  today - timedelta(days=1),
        '7d':  today - timedelta(days=7),
        '1mo': today - timedelta(days=30),
        '3mo': today - timedelta(days=91),
        '1yr': today - timedelta(days=365),
    }
    # Tighter tolerances for backfill decisions (prevents today's snapshot from
    # "satisfying" a target since they're only a few days apart). '1d' uses 0 so
    # only an exact-date prior snapshot counts — today's (1 day off) won't.
    backfill_tol = {'1d': 0, '7d': 3, '1mo': 7, '3mo': 14, '1yr': 21}

    for label, tgt in targets.items():
        tgt_str = tgt.strftime('%Y-%m-%d')
        tol = backfill_tol[label]

        if not nearest(snaps.get('wti', {}), tgt, tol=tol):
            print(f"\nBackfilling WTI {label} ({tgt_str})...")
            s = fetch_historical_strip('CL', tgt)
            if s:
                snaps['wti'][tgt_str] = s
                print(f"  → {len(s)} contracts")
            else:
                print("  → No data available (contract may not have existed yet)")

        if not nearest(snaps.get('hh', {}), tgt, tol=tol):
            print(f"Backfilling HH {label} ({tgt_str})...")
            s = fetch_historical_strip('NG', tgt)
            if s:
                snaps['hh'][tgt_str] = s
                print(f"  → {len(s)} contracts")
            else:
                print("  → No data available")

    save_snaps(snaps)

    # ── 4. Select display curves (closest snapshot to each target date) ────────
    display_targets = {'today': today, **targets}
    # 'today' and '1d' need exact-date matches so a missing yesterday snapshot
    # doesn't silently fall back to today's curve. Others allow loose matching.
    display_tol = {'today': 0, '1d': 0}

    def pick_curves(snaps_dict: dict) -> dict:
        return {
            label: {'date': key, 'data': snaps_dict[key]}
            for label, tgt in display_targets.items()
            if (key := nearest(snaps_dict, tgt, tol=display_tol.get(label, 14))) is not None
        }

    # ── 5. Write final JSON ───────────────────────────────────────────────────
    output = {
        'generated_at': datetime.now().isoformat(),
        'wti': {'spot_history': wti_spot, 'curves': pick_curves(snaps.get('wti', {}))},
        'hh':  {'spot_history': hh_spot,  'curves': pick_curves(snaps.get('hh',  {}))},
    }

    DATA_FILE.write_text(json.dumps(output, indent=2), encoding='utf-8')

    print(f"\n{'='*50}")
    print(f"Written → {DATA_FILE}")
    print(f"  WTI spot records : {len(wti_spot)}")
    print(f"  HH  spot records : {len(hh_spot)}")
    print(f"  WTI curve snaps  : {list(output['wti']['curves'].keys())}")
    print(f"  HH  curve snaps  : {list(output['hh']['curves'].keys())}")
    print("Done.")


if __name__ == '__main__':
    main()
