# Changelog

Notable user-facing and architectural changes. Dates are Africa/Johannesburg.

## 2026-11-23

### Correctness fixes (review round two)

- **Dividend income now uses the shares actually held on each ex-date**:
  the transaction log (buys/sells/splits/transfers) reconstructs position
  size before each ex-dividend. Top-ups no longer inflate income for
  earlier dividends, partial sells no longer overcount, and a buy ON the
  ex-date correctly earns nothing (was: current qty x all dividends
  since purchase).
- **Per-account "Sync Holdings" no longer writes USD values into Rand
  fields**: the Portfolio page's per-account button now goes through the
  shared sync service - the account type is preserved, USD rows convert
  at USD/ZAR, and a missing rate skips the row instead of storing
  dollars as Rand.
- **Stock splits are applied to holdings**: `apply_recent_splits` in
  `services/corporate_actions.py` resizes `quantity`/`purchase_price`
  per Yahoo split events (idempotent via a logged `split` transaction),
  runs inside Sunday's weekly cleanup over the last ~10 days, and via
  `run_updater.py --splits` for a one-year catch-up.
- **Cents-vs-Rand dividend guard gained a second signal**: an amount
  ~100x the symbol's median stored dividend is rescaled even when it
  passes the half-price check (a R2,500 share's 90c arrives as "90").
  The manual Add Dividend and Add Holding forms apply the same guard
  (purchase price >20x the near-date close -> cents) and say so in the
  success flash.
- An empty Yahoo response during the daily historical job now counts as
  a failure - a total feed outage can no longer record a successful
  `daily_historical` run and satisfy the catch-up gate.
- The Add Holding form blocks duplicate Manual-account positions
  instead of stacking phantom holdings.

### Reliability

- The cross-process job lock can no longer starve permanently: an
  exception while acquiring/releasing the OS file lock always frees the
  in-process gate (was: a single `mkdir`/`open` failure or a closed
  handle could freeze every future job for the process's lifetime).
- Startup schema migration is serialized under the job lock and each
  `ALTER TABLE`/`CREATE INDEX` tolerates a concurrent process applying
  it first (dashboard + daemon + one-shot used to race on startup).
- Daily backups run on a worker thread inside the daemon (pg_dump used
  to freeze the 60-second loop), filenames carry the PID (two concurrent
  backups could target the same file), and the whole backup takes the
  cross-process job lock.
- Windows service stop uses `shutdown(wait=False)` - `wait=True` could
  block minutes past the SCM timeout and get the service killed mid-job.
- A news-scraper failure inside the daily price+news job no longer fails
  the whole run (already-saved prices and the run record are kept);
  per-symbol dividend failures surface in the `daily_historical` run
  detail instead of being swallowed.
- CI gained a ruff static-analysis step (E9/F rules).

### Accuracy

- Alpha Vantage commodity backfill converts each bar at that day's
  USD/ZAR rate (was: a year of history converted at today's rate, which
  embeds the year's FX move as price error).
- `crypto_value` returns `None` when ANY held coin can't be priced -
  a partial sum read as complete net worth (same rule bullion already
  had). Metals.dev warns when `currency`/`unit` aren't ZAR/g.
- Home/net-worth value an entirely unpriced holding at cost basis, not
  zero - a just-added position no longer reports as a 100% loss.
- ADX uses Wilder smoothing (like RSI/ATR) instead of a plain SMA;
  VWAP's docstring now says it's cumulative-from-window-start, not a
  session VWAP.

### Pages

- Watchlist/portfolio/company post-action messages now render at the
  top of the page instead of inside collapsed expanders where users
  never saw them.
- Manual "Refresh Prices" clears the 60s price caches so the rerun
  shows the fresh values it just claimed, and the Twelve Data quota
  warning survives the rerun inside the flash message.
- Portfolio page: the 52w/trend stats and SENS status queries run once
  for all account groups (was once per group); Funds to Invest no
  longer caches a failed fetch as empty for the whole session; the raw
  EasyEquities debug button reports a failed fetch; CSV export captions
  that it covers the currently filtered rows.
- Commodity price charts load every series in one batched query cached
  5 minutes (was one SELECT per selected commodity per render), and the
  refresh flash says "refreshed N bars but X sources failed" instead of
  reading like a total failure.
- IPO table links are clickable `LinkColumn`s; the Daily Gain metric
  shows an explicit `+`/`-` sign; Krugerrands errors say "gold/silver",
  not `XAU`/`XAG`; Penny Stocks' Add-to-Watchlist reports the real
  add/skip outcome.
- Notification edge-trigger checks use one grouped query per run
  instead of a full-table scan per watchlist item; SENS dedup prefetch
  and `holding_statuses` are bounded to relevant windows.

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
