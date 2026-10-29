"""
Runs tools/validate_output.py after every `run` (the script's criteria are not ours to change):

    python tools/validate_output.py output/<date> holdings.csv

Exit code 0 = OK to upload. Anything else (or a missing script) = DO NOT UPLOAD: the FAIL lines are
printed, saved to output/<date>/validate.txt and put on the first line of alerts.md and the Telegram message.
"""
from __future__ import annotations

import os
import subprocess
import sys

from .config import ROOT

BANNER = "⛔ ห้ามอัปโหลด — DO NOT UPLOAD"


def _abs(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def _rel(path: str) -> str:
    """Relative to the repo root when inside it, so the command matches `output/<date> holdings.csv`."""
    try:
        rel = os.path.relpath(path, ROOT)
        return path if rel.startswith("..") else rel
    except ValueError:                       # different drive on Windows
        return path


def run(out_dir: str, cfg: dict) -> dict:
    vc = cfg["validator"]
    script, holdings = _abs(vc["script"]), _abs(vc["holdings_csv"])
    cmd_txt = f"python {_rel(script)} {_rel(out_dir)} {_rel(holdings)}"
    res = {"ok": False, "code": None, "cmd": cmd_txt, "fails": [], "warns": [], "summary": None, "output": "",
           "reason": None}
    if not os.path.exists(script):
        res["reason"] = f"{_rel(script)} not found — output was NOT validated"
        res["fails"] = [res["reason"]]
    else:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        try:
            p = subprocess.run([sys.executable, script, _rel(out_dir), _rel(holdings)], cwd=ROOT, env=env,
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=vc.get("timeout_seconds", 300))
            res["code"] = p.returncode
            res["output"] = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
            res["ok"] = p.returncode == 0
            lines = [ln.rstrip() for ln in res["output"].splitlines() if ln.strip()]
            res["warns"] = [ln for ln in lines if ln.startswith("WARN")]
            res["summary"] = next((ln for ln in reversed(lines) if ln.startswith("สรุป")), None)
            if not res["ok"]:
                res["fails"] = [ln for ln in lines if ln.startswith("FAIL")] \
                    or [ln for ln in lines if "FAIL" in ln.upper() and ln != res["summary"]] or lines[-10:] \
                    or [f"exit code {p.returncode} with no output"]
                res["reason"] = f"validate_output.py exit code {p.returncode}"
        except subprocess.TimeoutExpired:
            res["reason"] = "validate_output.py timed out"
            res["fails"] = [res["reason"]]
        except OSError as e:
            res["reason"] = f"could not start validate_output.py: {e}"
            res["fails"] = [res["reason"]]
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "validate.txt"), "w", encoding="utf-8") as f:
        f.write(f"$ {cmd_txt}\nexit code: {res['code']}\nresult: {'PASS' if res['ok'] else 'FAIL — ' + BANNER}\n")
        if res["reason"]:
            f.write(f"reason: {res['reason']}\n")
        f.write("\n" + (res["output"] or "(no output)") + "\n")
    return res


def banner_line(res: dict) -> str:
    return f"{BANNER}: {res['reason'] or 'validation failed'}"


def mark_alerts(out_dir: str, res: dict) -> None:
    """Prepend the DO-NOT-UPLOAD line (very first line) + the FAIL items to alerts.md."""
    path = os.path.join(out_dir, "alerts.md")
    old = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
    head = [f"**{banner_line(res)}**", "", "## Validation FAIL", *[f"- {x}" for x in res["fails"]],
            f"- full output: validate.txt (`{res['cmd']}`)", "", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(head) + old)


def print_result(res: dict) -> None:
    bar = "=" * 72
    if res["ok"]:
        for w in res["warns"][:10]:
            print(f"   ! {w}")
        if len(res["warns"]) > 10:
            print(f"   ! … อีก {len(res['warns']) - 10} ข้อ (ดู validate.txt)")
        if res["summary"]:
            print(f"   {res['summary']}")
        print(f"✅ validate_output.py ผ่าน (exit 0) — อัปโหลดได้   [{res['cmd']}]")
        return
    print(bar)
    print(banner_line(res))
    print(f"   คำสั่ง: {res['cmd']}")
    for x in res["fails"]:
        print(f"   ✗ {x}")
    if res["summary"]:
        print(f"   {res['summary']}")
    print("   รายละเอียดทั้งหมดอยู่ใน validate.txt ในโฟลเดอร์ output ของวันนี้")
    print("   → แก้ข้อ FAIL ก่อน แล้วค่อยอัปโหลด")
    print(bar)
