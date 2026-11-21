# Changelog

Notable user-facing and architectural changes. Dates are Africa/Johannesburg.

## 2026-11-21

### Data integrity (deep review)

- **USD accounts now stored in Rand**: EasyEquities USD-account holdings
  were written into Rand fields raw (values 18x off). Converted at `ZAR=X`
  during sync; rows show a "converted from USD" caption.
- **Commodity history unit seam fixed**: Alpha Vantage backfill wrote USD
  prices (per oz / per tonne) into ZAR/gram series - ~57x wrong bars next
  to Metals.dev snapshots. Converted at save; 998 legacy mixed bars
  deleted and re-backfilled.
- **One close convention everywhere**: latest-bar fetches used raw closes
  while history used adjusted - dividends produced phantom moves. All
  Yahoo paths now use adjusted close; `--normalize-closes` rewrites stored
  history (ran once: 40/41 symbols).
- Daily-gain % and "previous close" logic tightened: a suspended stock no
  longer presents a months-old close as yesterday's.

### Reliability

- Scheduled updater task: no execution-time limit (was silently killed at
  3 days) and restarts on failure. Re-run `install_updater_task.ps1` as
  Administrator to apply.
- Updater daemon jobs can no longer run concurrently on two threads or
  two processes (the job lock is now per-thread re-entrant).
- A crashed Windows service now restarts with a fresh scheduler instead
  of crash-looping on the dead one.
- Notifications created while delivery is disabled stay in-app instead of
  queuing a stale backlog; pending sends are capped to the last 24h.

### Pages

- Dividends page: "Symbols Paying" + "Dividend Income (since purchase)"
  metrics replace the meaningless sum-of-per-share-amounts figure.
- Krugerrands silver spot falls back to the last stored price on fetch
  failure (was silently blank).
- Home "Portfolio Value" delta clarified as gain since cost.
- "Scan SENS now", EE sync and watchlist/portfolio update messages no
  longer get eaten by `st.rerun()`.
- CoinGecko search waits for 3+ characters before hitting the API.

### Housekeeping

- Removed dead dependencies: `scipy`, `ta`, `streamlit-aggrid`.
- Daily dividend job skips symbols fetched within the last 7 days
  (manual fetch bypasses). Indicator recalc reads only OHLCV columns.

## 2026-11-20

### Branding

- Renamed the app from "JSE Stock Analysis Platform" to **AlphaLens**
  (browser tab, page header plus tagline, README, requirements header). The
  old name no longer fit - the app also tracks portfolio sync, crypto,
  bullion and net worth.

### Portfolio page

- New **Action** column on each holdings table - a rule-based suggestion
  per holding: `Sell - stop hit` / `Sell - no prices` /
  `Sell - still falling`, `Trim - target hit`, `Buy - recovering` /
  `Buy - uptrend`, `Hold` for everything else, `-` for non-JSE accounts.
  Inputs are stored 52-week/trend stats (one grouped query per account
  section, no extra fetches) plus your watchlist stop-loss/target as the
  exit rules. A loss alone is never a sell signal. Not financial advice.
- Removed the hidden `_gain_loss` helper column from the Portfolio and
  Watchlist tables - row colouring now reads the visible "Gain/Loss"
  column and still works when that column is hidden.
- `scripts/_holding_review.py`: terminal review printing holdings P/L
  plus the Action label per holding.

### Watchlist page

- Replaced the hidden `_above_target` helper column with a visible
  **To Target %** column - the signed distance to your target price
  (>= 0 = hit/exceeded, negative = room left). Sortable, and the
  darkgreen row colouring plus the "Above Target" filter now read it
  directly. Same behaviour, real information.

### Commodities page

- Added **aluminum, nickel, zinc and lead** spot prices from Metals.dev -
  they ride in the same `/v1/latest` response, so no extra quota. Stored
  as ZAR/metric tonne under `MD_*` pseudo-symbols; the precious metals
  stay ZAR/gram (`AV_*`). LBMA/MCX/IBJA benchmark symbols in the response
  are deliberately untracked.
- Removed the High/Low columns - neither source serves OHLC for these
  symbols, so they could never populate.
- All nine tracked metals are selectable in the comparison chart; base
  metals chart their accumulating daily snapshots.

## 2026-11-19

### Penny Stocks page

- Candidate universe widened from ~40 hand-picked tickers to ~290 probed
  JSE symbols (anything that traded below R10 in the last year, so names
  drifting toward R5 surface). Coverage caption shows how many returned
  data.
- New columns/fields: median daily **traded value** with score penalties
  for illiquid names, **staleness markers** for dead last-trades,
  per-row **Setup** tags - `Extended` / `Recovery - long` /
  `Bounce - short` / `Trend - mid` / `Weak` / `Watch` - and selected-stock
  **fundamentals** (fetched on demand, cached 6h).
- Holdings already owned are marked with a star on the Stock column.
- Price projection switched to log-linear with an uncertainty band
  (no more straight-line fantasy).
- Updater: near-52-week-low notifications for penny candidates
  (10% threshold, 30-day per-symbol cooldown).
- Dividend yield fix: each Yahoo dividend is now unit-checked against its
  own day's close - Rand-denominated payouts were being understated 100x.

### IPOs page

- New page scraping Moneyweb's public SENS feed, whitelisting real IPO
  phrases to catch upcoming listings during the offer window. New rows
  create notifications; first Yahoo bars flip a listing to "listed" and
  alert "now trading".
