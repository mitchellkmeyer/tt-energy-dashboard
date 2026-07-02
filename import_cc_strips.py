"""
Import ComboCurve historical weekly strips into curve_snapshots.json.

ComboCurve export (long format): columns Futures Date | Key | Period | Value | Unit
(Key/Unit filled only on each block's first row -> forward-fill). Key "Oil"=WTI
($/BBL), "Gas"=HH ($/MMBTU); Period is the delivery month (first-of-month).

Merge policy (one source per era): ComboCurve is authoritative for the HISTORICAL
curves. All existing snapshots dated <= ComboCurve's max date are dropped and
replaced by ComboCurve; existing snapshots after that (our live Yahoo capture)
are kept untouched. Dry-run by default; pass --apply to write.
"""
import sys, json
import pandas as pd
from pathlib import Path

DIR = Path(__file__).parent
SNAP = DIR / 'curve_snapshots.json'
XLSX = DIR / 'CC Weekly Historical Strips from 07.2025-06.2026.xlsx'
HORIZON = 60                          # keep first N monthly deliveries per strip
KEYMAP  = {'Oil': ('wti', 2), 'Gas': ('hh', 3)}   # cc key -> (our key, price decimals)

def load_cc():
    df = pd.read_excel(XLSX, sheet_name='Sheet1')
    df.columns = [str(c).strip() for c in df.columns]
    df['Futures Date'] = pd.to_datetime(df['Futures Date'])
    df['Period'] = pd.to_datetime(df['Period'])
    df['Key'] = df['Key'].ffill()
    out = {'wti': {}, 'hh': {}}
    for cc_key, (our_key, dec) in KEYMAP.items():
        sub = df[df['Key'] == cc_key]
        for fdate, grp in sub.groupby('Futures Date'):
            dstr = pd.Timestamp(fdate).date().isoformat()
            grp = grp.sort_values('Period').drop_duplicates('Period')
            rows = [{'delivery': pd.Timestamp(p).strftime('%Y-%m'),
                     'price': round(float(v), dec)}
                    for p, v in zip(grp['Period'], grp['Value'])][:HORIZON]
            out[our_key][dstr] = rows
    return out

def main():
    apply = '--apply' in sys.argv
    cc = load_cc()
    snaps = json.loads(SNAP.read_text(encoding='utf-8'))

    cc_dates = sorted({d for k in cc for d in cc[k]})
    cutoff = cc_dates[-1]
    print(f"ComboCurve: {len(cc_dates)} dates {cc_dates[0]} … {cutoff}  "
          f"(horizon {HORIZON} mo)")

    for key in ('wti', 'hh'):
        existing = snaps.get(key, {})
        removed = sorted(d for d in existing if d <= cutoff)
        kept    = sorted(d for d in existing if d > cutoff)
        merged = {d: existing[d] for d in kept}
        merged.update(cc[key])
        snaps[key] = dict(sorted(merged.items()))
        print(f"\n[{key}] existing={len(existing)}  drop(<= {cutoff})={len(removed)}  "
              f"keep(> {cutoff})={len(kept)}  +CC={len(cc[key])}  -> total={len(snaps[key])}")
        print(f"      kept (live) dates: {kept}")

    # seam check: last CC date vs first kept live date
    print("\n--- SEAM: ComboCurve (last) vs Yahoo-live (first kept) ---")
    for key in ('wti', 'hh'):
        kept = sorted(d for d in json.loads(SNAP.read_text(encoding='utf-8')).get(key, {})
                      if d > cutoff)
        if not kept:
            continue
        cc_last = cc[key][cutoff]
        yh = json.loads(SNAP.read_text(encoding='utf-8'))[key][kept[0]]
        yh_map = {r['delivery']: r['price'] for r in yh}
        print(f"  {key}: CC {cutoff}  vs  Yahoo {kept[0]}")
        for r in cc_last[:6]:
            d = r['delivery']; y = yh_map.get(d)
            diff = f"{r['price']-y:+.2f}" if y is not None else "n/a"
            print(f"     {d}  CC={r['price']:<8} Yahoo={y if y is not None else '-':<8} diff={diff}")

    if apply:
        SNAP.write_text(json.dumps(snaps, indent=2), encoding='utf-8')
        print(f"\nAPPLIED -> {SNAP}")
    else:
        print("\n(dry-run — no file written; pass --apply to write)")

if __name__ == '__main__':
    main()
