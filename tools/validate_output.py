"""ตรวจไฟล์ output ของ portfolio_stage ก่อนอัปโหลดเข้าหน้า rebalance
ใช้: python validate_output.py output/2026-10-29 [holdings.csv]
"""
import csv, json, sys, datetime as dt
from pathlib import Path

def bizdays(a, b):
    n, d = 0, a
    while d < b:
        d += dt.timedelta(days=1)
        if d.weekday() < 5: n += 1
    return n

def main(folder, holdings=None):
    f = Path(folder); res = []
    ok = lambda t: res.append(("OK  ", t)); warn = lambda t: res.append(("WARN", t)); fail = lambda t: res.append(("FAIL", t))
    held = []
    if holdings and Path(holdings).exists():
        held = [r["ticker"].strip().upper() for r in csv.DictReader(open(holdings, encoding="utf-8"))]
    # ---- ohlc.json
    try:
        O = json.load(open(f / "ohlc.json", encoding="utf-8")); T = O["tickers"]; ok(f"ohlc.json อ่านได้ {len(T)} ตัว")
    except Exception as e:
        fail(f"ohlc.json อ่านไม่ได้: {e}"); T = {}
    if T:
        latest = max(r[-1][0] for r in T.values() if r)
        lag = bizdays(dt.date.fromisoformat(latest), dt.date.today())
        (ok if lag <= 1 else warn if lag <= 3 else fail)(f"ราคาล่าสุด {latest} ห่างวันนี้ {lag} วันทำการ")
        (ok if "SPY" in T else fail)("มี SPY" if "SPY" in T else "ไม่มี SPY")
        miss = [h for h in held if h not in T]
        if held: (fail if miss else ok)(f"ขาด {miss}" if miss else f"ครบทุกตัวใน holdings ({len(held)})")
        for k, rows in T.items():
            if len(rows) < 200: warn(f"{k}: ประวัติ {len(rows)} วัน (<200)")
            if rows and rows[-1][0] < latest: warn(f"{k}: ราคาล่าสุดแค่ {rows[-1][0]}")
            prev = None
            for r in rows:
                d, o, h, l, c = r[:5]
                if not (c > 0 and h >= max(o, c) - 1e-6 and l <= min(o, c) + 1e-6):
                    fail(f"{k}: OHLC ผิดรูปวันที่ {d}"); break
                if prev and d <= prev: fail(f"{k}: วันที่ไม่เรียงที่ {d}"); break
                if prev and (dt.date.fromisoformat(d) - dt.date.fromisoformat(prev)).days > 5: warn(f"{k}: ข้อมูลขาดช่วง {prev}→{d}")
                prev = d
            for a, b in zip(rows, rows[1:]):
                if b[4] / a[4] - 1 < -0.45 or b[4] / a[4] - 1 > 1.0:
                    warn(f"{k}: ราคาเปลี่ยน {((b[4]/a[4])-1)*100:+.0f}% วันที่ {b[0]} (แตกพาร์หรือไม่ได้ใช้ราคา adjusted?)")
    # ---- portfolio_stage.json
    try:
        S = json.load(open(f / "portfolio_stage.json", encoding="utf-8")); S = S.get("tickers", {k: v for k, v in S.items() if k != "meta"})
        ok(f"portfolio_stage.json อ่านได้ {len(S)} ตัว")
        for k, v in S.items():
            lack = [x for x in ("p", "stage", "stage_days") if v.get(x) is None]
            if lack: warn(f"{k}: ไม่มี {lack}")
            if v.get("stage") not in (None, 1, 2, 3, 4): fail(f"{k}: stage ต้องเป็น 1–4 ได้ {v.get('stage')}")
            if T.get(k) and v.get("p") and abs(v["p"] / T[k][-1][4] - 1) > 0.01: fail(f"{k}: p ไม่ตรงกับราคาปิดใน ohlc.json")
            q = v.get("quality")
            if q and any(not (0 <= float(q.get(x, -1)) <= 5) for x in ("growth", "profitability", "consistency", "balance_sheet", "valuation")):
                warn(f"{k}: quality ต้องมีครบ 5 ช่อง ค่า 0–5")
    except FileNotFoundError:
        warn("ไม่มี portfolio_stage.json (หน้าคำนวณ Stage จาก ohlc.json เองได้)")
    except Exception as e:
        fail(f"portfolio_stage.json อ่านไม่ได้: {e}")
    for lvl, t in res: print(lvl, t)
    nf = sum(1 for l, _ in res if l == "FAIL"); nw = sum(1 for l, _ in res if l == "WARN")
    print(f"\nสรุป: ไม่ผ่าน {nf} · เตือน {nw} → {'อัปโหลดได้' if nf == 0 else 'แก้ข้อ FAIL ก่อน'}")
    sys.exit(1 if nf else 0)

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
