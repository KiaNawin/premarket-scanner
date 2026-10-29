"""Send the daily alerts to Telegram (same bot and .env as the scanner)."""
from __future__ import annotations

import html
import re

from .report import ALERT_ICONS, grouped_alerts


def _md_to_html(text: str) -> str:
    """alerts.md uses **TICKER** for bold — convert to Telegram HTML safely."""
    out = html.escape(text, quote=True)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    return re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', out)


def build_message(meta: dict, alerts: list[tuple[str, str]], rows: dict) -> str:
    total = meta.get("total_value")
    head = [f"<b>📒 Portfolio — {meta['as_of']}</b>",
            f"Value {'—' if total is None else f'${total:,.0f}'} · {meta.get('positions', 0)} positions · "
            f"weekly bars to {meta.get('weekly_bars_through', '—')}"]
    s2 = sum(1 for r in rows.values() if r.get("stage") == 2)
    s4 = sum(1 for r in rows.values() if r.get("stage") == 4)
    ok = sum(1 for r in rows.values() if r.get("flag") == "✅")
    warn = sum(1 for r in rows.values() if r.get("flag") == "⚠️")
    head.append(f"Stage 2: {s2} (✅ {ok} · ⚠️ {warn}) · Stage 4: {s4}")
    miss = meta.get("missing_tickers") or {}
    if miss:
        head.append(f"⚠️ No data for {len(miss)} ({html.escape(', '.join(miss))}) — totals and analysis are incomplete")
    lines = head + [""]
    if not alerts:
        lines.append("Nothing to check today ✅")
    else:
        for kind, items in grouped_alerts(alerts):
            lines.append(f"<b>{ALERT_ICONS.get(kind, '•')} {html.escape(kind)}</b> ({len(items)})")
            lines += [f"• {_md_to_html(x)}" for x in items]
            lines.append("")
    lines.append(f"<i>Details: output/{meta['as_of']}/summary.md</i>")
    return "\n".join(lines).strip()


def send(text: str, silent: bool = False) -> bool:
    """Telegram link previews are disabled by telegram_notify, so news links stay compact."""
    import telegram_notify as tg          # loads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from .env
    return tg.send_telegram(text, silent=silent)


def send_failure(err: str) -> None:
    try:
        send(f"<b>❌ Portfolio run failed</b>\n<code>{html.escape(err[-1500:])}</code>\n"
             "Log: output/portfolio_run.log")
    except Exception:
        pass
