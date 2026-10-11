# Pre-market Gap Scanner

Scans every US-listed common stock (NASDAQ, NYSE, NYSE American) before the open and keeps those with:

| Filter | Default | Flag |
|---|---|---|
| Gap up vs. previous close | > 5% | `--min-gap` |
| Latest pre-market price | > $3 | `--min-price` |
| Pre-market volume (04:00 ET → now) | > 50,000 shares | `--min-volume` |

**Data:** prices, gap % and pre-market volume come from Yahoo Finance 1-minute bars (via `yfinance`). The one-sentence catalyst comes from Benzinga — through the Benzinga News API when `BENZINGA_API_KEY` is set, otherwise from Benzinga stories in Yahoo's ticker news feed (falling back to the latest headline from any source).

## Quick start

```bash
pip install -r requirements.txt
python premarket_scanner.py                        # full scan (~7,000 symbols, a few minutes)
python premarket_scanner.py --tickers APLD,PLTR    # quick scan of specific tickers
```

Example output:

```
Ticker       Price    Gap %   Catalyst
-----------------------------------------------------------------------
BIG         $26.00  +30.00%   Shares jump after FDA approval of lead drug.
GOOD        $11.00  +10.00%   Company beats Q3 estimates and raises guidance.
```

Each run writes `scans/premarket_scan_YYYY-MM-DD.json`:

```json
{
  "scan_date": "2026-10-09",
  "generated_at_et": "2026-10-09 09:00:12 EDT",
  "criteria": {"min_gap_pct": 5.0, "min_price": 3.0, "min_premarket_volume": 50000},
  "symbols_scanned": 6912,
  "count": 2,
  "results": [
    {"ticker": "BIG", "price": 26.0, "gap_pct": 30.0,
     "catalyst": "Shares jump after FDA approval of lead drug.",
     "prev_close": 20.0, "premarket_volume": 160000,
     "catalyst_source": "Benzinga", "catalyst_headline": "...", "catalyst_url": "..."}
  ]
}
```

When it re-runs after the 09:30 open, **Price / Gap %** use the latest trade (vs. yesterday's close) and the volume filter stays on **pre-market** volume. Each run overwrites that day's file with the latest list.

---

# Automation + Telegram (`run_cycle.py`)

## Schedule (New York time, US trading days; NYSE holidays skipped)

| | Runs | Bangkok time (until 1 Nov) | Bangkok time (after 1 Nov) |
|---|---|---|---|
| Scanner A — pre-market gappers | 08:30, 09:00 … 14:00 (every 30 min, 12 runs) | 19:30 → 01:00 | 20:30 → 02:00 |
| Scanner B — Trend-join Long | 08:35, 09:05 … 14:35 (5 min after each A, last before 15:00, 13 runs) | 19:35 → 01:35 | 20:35 → 02:35 |

After **every Scanner B run** your bot gets one message:

```
[Scanner A: Pre-market Gappers]
🕒 Scan 10:00 ET · 2026-10-09
Filters: gap >5% · price >$3 · PM vol >50K

1. APLD $31.25 +8.20% · PM vol 1.2M
   📰 Applied Digital beat Q1 estimates.
2. …

[Scanner B: Trend-join Long Hits]
🕒 Scan 10:05 ET · 1 valid entry / 6 tickers

✅ APLD — LONG entry $31.25 @ 10:07 ET
   Y-High $30.90 · PM-High $31.10 · HOD $31.18 · SMA200 $21.70
   Last $31.40 · Gap +8.20%
   📰 Applied Digital beat Q1 estimates.

Watching: LEVI (< PM-High); BULL (< SMA200)
```
Before 10:00 ET Scanner B shows the candidates as "waiting 10:00". Long lists are split across messages automatically.

## 1. Create the Telegram bot (2 minutes)
1. In Telegram, open **@BotFather** → `/newbot` → follow the prompts → copy the **token** (`123456:ABC…`).
2. Open your new bot and send it any message (e.g. `hi`).
3. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser → find `"chat":{"id": 123456789 …}` → that number is your **chat id**. (For a group, add the bot to the group, post a message, and use the negative id.)
4. Test:
   ```bash
   export TELEGRAM_BOT_TOKEN=123456:ABC...   # Windows: set TELEGRAM_BOT_TOKEN=...
   export TELEGRAM_CHAT_ID=123456789
   python telegram_notify.py --test
   ```

## 2. Run it — pick one

**Option A — always-on computer or VPS (exact timing, recommended)**
```bash
python run_cycle.py --loop
```
It sleeps until each slot, handles US daylight saving and holidays by itself. Keep it running with `tmux`/`screen`, a systemd service, or Windows Task Scheduler "at log on".

**Option B — GitHub Actions (no computer needed)**
1. Push this folder to a GitHub repo.
2. Settings → Secrets and variables → Actions → add `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (and optionally `BENZINGA_API_KEY`).
3. `.github/workflows/scanners.yml` fires every 30 min and runs the matching slot; results are committed to `scans/`, `signals/`, `watchlists/`. Use **Actions → Scanners → Telegram → Run workflow** (optionally with slot `10:00`) to test.
   * GitHub can start scheduled jobs a few minutes late; Scanner B then runs right after Scanner A.
   * ~13 runs/day uses roughly 2,000+ Actions minutes/month — free for **public** repos; a private repo may exceed the free 2,000 minutes.

## Useful commands
```bash
python run_cycle.py --show-schedule          # today's slots in ET and Bangkok time
python run_cycle.py --slot 10:00 --dry-run   # run one cycle now, print the message instead of sending
python run_cycle.py --slot 10:00 --tickers APLD,PLTR   # quick cycle on a few tickers
```

---

# Trend-join Long — Strategy Scanner

Filters the pre-market gappers above and flags a **VALID ENTRY** when, **after 10:00 AM ET**, all of these pass on a 1-minute bar:

1. Price > yesterday's daily high
2. Yesterday's close > daily 200 SMA
3. Price > today's pre-market high (04:00–09:30 ET)
4. Price > current high of day — the bar closes at a new regular-session high

The first bar that passes all four is the signal (time + entry price are recorded).

## Two ways to run it (same logic in both)

### 1. TradingView — `tradingview/trend_join_long.pine`
1. **Import the day's gappers:** Watchlist panel → `⋯` → *Import list…* → pick `watchlists/tradingview_YYYY-MM-DD.txt` (written automatically after the pre-market scan; or run `python strategy_scanner.py --watchlist-only`).
2. **Add the script:** Pine Editor → paste the file → *Save* → *Add to chart*. Use a **1–5 min chart with extended hours ON** (needed for the pre-market high). Add it to *Favorites*.
3. **Scan the whole list at once:** Products → *Pine Screener* → choose the watchlist and the *Trend-join Long Scanner* indicator → timeframe 1m → filter `Signal fired today = 1`.
4. **Automated alerts:** on a chart, *Create alert* → condition *Trend-join Long Scanner* → **Any alert() function call** → enable *Webhook URL* if you want the JSON pushed to a server. Payload:
   ```json
   {"strategy":"Trend-join Long","signal":"VALID_ENTRY","ticker":"APLD","exchange":"NASDAQ",
    "price":31.25,"time_et":"2026-10-09T10:07:00","yesterday_high":30.9,"yesterday_close":30.4,
    "sma200":21.7,"premarket_high":31.1,"hod_before_signal":31.18}
   ```
   (TradingView alerts are per-symbol; one alert per ticker, or use the Pine Screener for the list. Webhooks need a paid TradingView plan.)

The chart also shows the three levels, a green **TJL** marker on the signal bar, and a ✅/❌ checklist table.

### 2. Python, fully automated — `strategy_scanner.py`
```bash
python strategy_scanner.py            # reads the latest scans/premarket_scan_*.json, must be 10:00–16:00 ET
python strategy_scanner.py --force    # run at any time (testing)
```
Writes, for automated reporting:
* `signals/trend_join_long_YYYY-MM-DD.json` — valid entries + every candidate's per-criterion result
* `signals/trend_join_long_YYYY-MM-DD.md` — ready-to-post summary table
* if `WEBHOOK_URL` is set → POSTs `{"text": <report>, "content": <report>, "report": <json>}` (works with Slack and Discord incoming webhooks)

Statuses: `VALID_ENTRY`, `NO_SIGNAL` (with which criteria fail now), `FAIL` (below 200 SMA), `WAITING` (before 10:00), `NO_DATA`.

### Schedule
Scheduled automatically by `run_cycle.py` (see *Automation + Telegram* above): every 30 min from 08:35 to 14:35 ET, each run re-checking the whole day so far, so signals that trigger at any time after 10:00 are caught.

## Notes
* Benzinga News API keys come from benzinga.com/apis (paid; a trial is available). Without one, the script still pulls Benzinga stories where Yahoo carries them.
* Yahoo data via `yfinance` is unofficial and for personal use; it can occasionally rate-limit — failed batches are skipped and the scan continues.
* Not investment advice.
