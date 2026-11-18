# AlphaLens Agent Notes

Context for anyone working on this repository in the future.

## Project overview

- **Name:** AlphaLens (JSE Stock Analysis Platform)
- **Stack:** Python 3.13+, Streamlit, SQLAlchemy 2, APScheduler, Pydantic Settings, Loguru, Twelve Data API, Yahoo Finance, RSS feeds
- **Repository root:** `C:\Repos\AlphaLens`
- **Entry points:**
  - `main.py` – Streamlit dashboard
  - `scripts\run_updater.py` – headless updater (Task Scheduler / Windows Service)

## Environment setup

1. Create and activate a virtual environment in the repo root (`.venv` or `venv`).
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in real values.

## Database

- **Default:** `sqlite:///./data/stock_analyzer.db` (good for single-user desktop)
- **Production / always-on:** PostgreSQL.
- **PostgreSQL setup on Windows:**
  - `psql` is not on `PATH` by default; the install path for PG 18 is `C:\Program Files\PostgreSQL\18\bin\psql.exe`.
  - Bootstrap DB/user: `& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -f scripts\setup_postgres.sql`
  - Driver: `psycopg2-binary>=2.9.0,<3.0` is in `requirements.txt`.
  - Update `.env`: `DATABASE_URL=postgresql://<user>:<pass>@localhost:5432/<db>`
  - Tables are created automatically via `python -c "from app.database.init_db import init_database; init_database()"`.
  - **Session timezone must be UTC.** The DateTime columns are naive and the app treats stored values as UTC; a non-UTC session timezone silently converts aware values to local wall-clock on write (showing times 2h ahead). `scripts\setup_postgres.sql` runs `ALTER DATABASE ... SET timezone = 'UTC'`, and `app\models\base.py` also passes `-c timezone=UTC` in connect_args. If timestamps were already stored shifted, run `python scripts\fix_postgres_timezone.py --apply` once.

## Schema changes and backups

- No Alembic. `init_database()` runs `create_all` (new tables) then `_migrate_schema()` in `app/database/init_db.py`. `_migrate_schema` generically diffs model columns vs `inspect()` and ADDs whatever is missing (dialect-rendered specs, valid on SQLite and PostgreSQL) — new columns need no migration step. Index/constraint enforcement and data repairs still go in `_enforce_unique_indexes`/`_ensure_plain_indexes`/the schema_meta-gated repair helpers.
- `app/services/backup.py`: the daemon's 60s loop calls `_backup_if_due()` - backs up when the newest file in `data/backups/` is >7 days old (survives sleep, unlike a cron slot), keeps 8, records `database_backup` in `updater_runs`. `run_updater.py --backup` forces one. Take one before any bulk data repair. Every backup is verified before it's kept: SQLite via `PRAGMA integrity_check` on the copy, PostgreSQL via `pg_restore -l` on the archive (skipped when pg_restore isn't installed).
- `scripts/check_stored_prices.py [--all]` - read-only stored-vs-Yahoo close check. As of 2026-11-18 every tracked symbol Yahoo carries agrees; SKA (R0.0085), BAT and AXX were verified correct (backlog #5 was wrong).
- `scripts/sync_easyequities.py` - headless EasyEquities holdings sync (same code path as the Portfolio page; it was `_sync_holdings.py` in the repo root).

## Windows headless updater

- `scripts\run_updater.py` (via `app\services\updater.py`) is the **only** scheduler in this app - it owns all price/news/historical/dividend fetching. `main.py` (the Streamlit dashboard) does not run a background scheduler of its own; it only reads from the database and offers a manual "Refresh Prices" button. An in-process `SchedulerService` used to run inside the dashboard too (hourly, no quota/lock safeguards) - it duplicated the headless updater's work against the same shared Twelve Data quota and was removed. Do not reintroduce a scheduler inside `main.py`.
- **Recommended:** Task Scheduler via `scripts\install_updater_task.ps1` (run PowerShell as Administrator).
- The script prefers `.venv\Scripts\python.exe` or `venv\Scripts\python.exe`, then falls back to `python` in `PATH`.
- Correct trigger parameter for logon is `-AtLogon`, not `-Logon`.
- Schedule:
  - `09:00, 13:00, 17:00` – price + news (`--price-news`)
  - `18:00` – full update (`--full`)
  - logon + workstation unlock – lightweight refresh (`--logon`, debounced).
    The unlock trigger is a `SessionStateChangeTrigger` (`StateChange = 8`)
    built via `MSFT_TaskSessionStateChangeTrigger` CIM; `-AtLogon` alone does
    not fire on unlock.
- **Alternative:** `scripts\windows_service.py` using `pywin32` (`--install`, `--start`, etc.).

## Running the updater manually

```powershell
cd C:\Repos\AlphaLens
python scripts\run_updater.py --help
python scripts\run_updater.py --price-news
python scripts\run_updater.py --full
python scripts\run_updater.py --logon
```

## API rate limits

- **Twelve Data** free tier: 8 credits/minute, 800/day. A request costs **one credit per symbol** - a 10-symbol batch quote is 10 credits and is rejected outright on the free tier. Quote batches are capped at the per-minute limit (`max_batch_size()`), and every attempt including retries is throttled and counted.
- The app throttles automatically and falls back to **Yahoo Finance** for prices, history, and dividends.
- **The free plan serves no JSE symbols** (checked 2026-11-18: SOL/AXX need the Pro plan, NPK resolves to NYSE). In the whole log history every price/history/dividend came from Yahoo. `TWELVE_DATA_ENABLED=false` skips Twelve Data entirely (no requests, no quota caption); the cost of leaving it on is 429 noise and ~12 min of rate-limit sleeping per daily historical run. With it off, `_close_corroborated` has no second source, so implausible moves are always rejected (same as before in practice).

## Common commands

```powershell
# Verify settings load
python -c "from app.config.settings import settings; print(settings.database_url)"

# Init database (works for both SQLite and PostgreSQL)
python -c "from app.database.init_db import init_database; init_database()"

# Run dashboard
streamlit run main.py

# Install scheduled updater task
.\scripts\install_updater_task.ps1

# Query the task
schtasks /query /tn AlphaLensUpdater
schtasks /run /tn AlphaLensUpdater

# Uninstall the task
.\scripts\uninstall_updater_task.ps1
```

## Known quirks

- `psql` is not on `PATH`; use the full `C:\Program Files\PostgreSQL\<version>\bin\psql.exe` path.
- The PostgreSQL `postgresql-x64-18` service may not be startable/stoppable from a non-elevated PowerShell session; admin rights are required.
- `scripts\install_updater_task.ps1` must be run from the repo root and as Administrator. The correct design is two tasks: `AlphaLensUpdater` (daemon) and `AlphaLensUpdaterLogon` (`--logon`). A single task with multiple actions runs every action on every trigger, which is wrong.
- Docker may be installed but the engine may not be running; verify before relying on a container.
- The `updater.log` file is written to `data/logs/updater.log`; the parent directory is created automatically.
- The JSE index symbol `^J203.JO` must be explicitly tracked by the updater; otherwise the Home page "last market data" caption goes stale.

## SQLite to PostgreSQL migration

If a user switches `DATABASE_URL` to PostgreSQL after using SQLite, the old data in `data/stock_analyzer.db` is not copied automatically. Use:

```powershell
python scripts\migrate_sqlite_to_postgres.py
```

It truncates the Postgres tables and copies all rows from the SQLite file, preserving primary keys and resetting `id` sequences. It is idempotent.

## Planned feature: Upcoming JSE IPOs / new listings

Researched 2026-11-17; not yet built. Goal: alert the user when a company IPO is
announced so they can subscribe during the offer window (before listing day).

- **Source:** Moneyweb SENS archive at
  `https://www.moneyweb.co.za/tools-and-data/moneyweb-sens/` — public (no login
  needed), lists every SENS announcement with timestamp, ticker, headline, and
  link to the official `senspdf.jse.co.za` PDF. Official JSE SENS feed is paid.
- **Filter:** whitelist real-IPO phrases: "proposed listing", "pre-listing
  statement", "intention to list", "admission to listing", "initial public
  offer". Must EXCLUDE daily noise: "New Financial Instrument Listing"
  (warrants/notes), "Listing of Additional ... Securities", interest payments.
  Genuine company IPOs are ~5-10/year, so precision > recall.
- **Pieces** (all patterns already exist):
  - Model `app/models/upcoming_listing.py` — symbol, name, listing_date,
    offer_open/close, status, unique `source_url` for dedup. `create_all` makes
    the table; add indexes to `_ensure_plain_indexes` if needed.
  - `MoneywebCollector.get_sens_announcements()` — reuse its session/retry/
    BeautifulSoup pattern.
  - `UpdaterService._update_ipo_listings()` daily ~08:00, modelled on
    `_weekly_cleanup`, `_record_run(db, "ipo_listings", ...)`; optional `--ipo`
    flag in `run_updater.py`.
  - `NotificationService.create_notification(type="ipo", ...)` — dedup and
    delivery (email/ntfy/Discord/Telegram) come free; add icon to
    `_TYPE_ICONS` in `app/dashboard/notifications.py`.
  - Page `app/dashboard/upcoming_ipos.py` + a `_PAGES` entry and a
    `PAGE_DATA_SOURCES` entry in `app/dashboard/home.py`.
  - On listing day: probe `<symbol>.JO` via `yf.download` (penny-stocks batch
    pattern); first bars -> status "listed", create `Stock` row via
    `get_company_name`, notify "now trading".
- **Phase 2:** the EasyEquities collector could surface their retail IPO offers.
- **Caveat for users:** this gives early warning only — subscribing still goes
  through a broker during the offer window in the pre-listing statement.

## Cross-DB SQL expressions

The `strftime('%Y-%m-%d', timestamp)` function is SQLite-only. For PostgreSQL, use `func.date_trunc('day', timestamp)`. The `app/dashboard/utils.py` helper `day_of(db, col)` selects the right one from the session's engine dialect (not `settings.database_url`, so SQLite tests work against a Postgres-configured `.env`).

## Data-write conventions

- Zero/negative OHLC values are treated as missing (`_apply_price_values`) - Yahoo history uses `auto_adjust=True` and has no `Adj Close`, so `adjusted_close` falls back to `close`. 13,365 legacy `adjusted_close = 0.0` rows were repaired 2026-11-18.
- Daily bars are keyed by naive exchange-local midnight (`YYYY-MM-DD 00:00`), not UTC instants. Re-fetches overwrite the same bar in place — never insert a duplicate row for the same `(symbol, timestamp)`; that pair has a unique index.
- All `DateTime` columns are naive and store UTC. Use `utcnow_naive()` from `app/models/base.py` for defaults/writes — never write `datetime.now(timezone.utc)` (aware) into a naive column; SQLite would store a `+00:00` suffix and break ordering.
- Save helpers in `data_service.py` (`_save_price_from_quote`, `_save_recent_bars`) return `bool` and roll back on error. `_save_recent_bars` writes every bar in the fetched Yahoo frame (gap-filling missed days), not just the latest. `update_stock_prices`/`update_dividends` **require** an explicit symbol list (no all-stocks fallback — that was a quota trap); they batch Twelve Data quotes 10-per-request and Yahoo fallbacks via `yf.download`. Batched quotes must carry `mic_code == XJSE` per result — same strict check as `get_quote`.
- `_get_tracked_symbols` (updater) returns watchlist + JSE-eligible portfolio symbols **intersected with `Stock.is_active`** — a deactivated stock is not fetched even if still on a watchlist. Deactivation is the bad-ticker/delisting kill switch: the add paths set it automatically when a symbol has no backfill and no stored prices, re-adding reactivates, and Company View's "Manage tracked stocks" expander toggles it manually.
- `update_historical_data` is incremental: once a backfill exists it fetches only ~the last stored bar + 7-day overlap, not the full window. The daily job calls `update_historical_data_batch(symbols)` — same per-symbol logic (single-symbol version remains for the dashboard add paths) but one grouped stats query + one batched `yf.download` (`get_history_batch`) for all Yahoo fallbacks. `_update_technical_indicators(symbol, full=?, tail=N)` — `full=True` recomputes all history (backfills); `tail` controls how many trailing bars get persisted indicator values on incremental runs (pass `new_bars + 1`).
- Implausible closes (>3x vs previous stored close, `MAX_CLOSE_RATIO`) are cross-checked against the *other* provider before rejection (`_close_corroborated`): corroborated moves (crashes, splits/consolidations) are accepted, uncorroborated ones rejected, and both raise a `type="system"` in-app Notification. Corroboration applies to both single-bar and bulk-save paths.
- JSE dividend amounts may be cents or Rand per ticker on BOTH providers; `update_dividends` rescales amounts >50% of the contemporaneous share price (within 730 days of ex-date) to Rand for Yahoo and Twelve Data alike. Dividend dedup is per (calendar day, amount) — a special dividend can share an ex-date with an interim, so the unique index is on all three columns. `scripts/fix_dividend_amounts.py --apply` already repaired legacy duplicates/cent amounts once; don't re-run it blindly.
- EasyEquities amounts: `_ee_reconcile_units` checks the implied price vs the stored JSE close on JSE-tracked accounts and rescales obvious ZAc/ZAR mismatches (/100) — the parser's currency code is kept on the holding dict (`"currency"`); non-ZAR amounts on JSE accounts are logged and skipped, not trusted.
- Alert Notification rows are created on every check run regardless of `notification_enabled` (the dashboard page is an in-app channel); only external delivery via Apprise is gated on the setting.
- `get_latest_and_previous_prices` (`services/price_queries.py`) scopes its window CTE to 92 days back from each symbol's own latest bar — don't widen it without a reason; symbols suspended longer than that simply get no previous-day snapshot.
- Portfolio value history: `portfolio_snapshots` holds one row per portfolio per SAST day (unique), upserted by `services/portfolio_valuation.update_portfolio_totals` - called by the Portfolio page and, via `revalue_portfolio`, after every updater price run, so the series fills in even when the dashboard isn't opened. Revaluation lives in that Streamlit-free module; the page's `_update_holding_values` is a thin wrapper passing its cached price lookup.
- `_ensure_stock` backfills sector/industry/market_cap/shares/description/website from Yahoo `get_info` once per stock — gated on `details_fetched_at` (stamped on the first successful profile fetch, even a sector-less one; NULL after a failed fetch so transient outages retry). Never call `get_info` in a save path without checking it.
- One-time data repairs in `init_db.py` are gated on `schema_meta` keys (e.g. `normalize_bar_timestamps_v1`) — checked via a PK lookup so they don't scan the table on every dashboard/daemon startup. New repair passes should add their own key, not re-run unconditionally.
- RSI/ATR use Wilder smoothing (`ewm(alpha=1/n)`). All stored history was recomputed (`full=True`) on 2026-11-18, so it is uniformly Wilder. Commodity snapshot series (`AV_*`, `XAU_*`) are close-only, so high/low indicators (ADX etc.) log a calculation error for them - expected.
- USD-account portfolio holdings (`is_jse_tracked_account` == False) are never priced/valued from JSE bars — `_update_holding_values` and Home's metrics only apply stored JSE closes to JSE-tracked accounts; EE-synced USD values pass through untouched. Holdings priced from `account_type` appear in Rand totals everywhere.
- `PortfolioHolding.account_type == "Manual"` marks dashboard-added holdings — EasyEquities reconciliation deletes any holding under an EE-reported account type that EE stops reporting, so manual rows must never carry a real EE account name or the next sync deletes them.
- When a JSE price lookup finds no stored bar, holdings keep their last-known (EE-synced) values — never null fields on a missing fetch.
- The updater writes a heartbeat row to the `updater_runs` table on every job completion (`_record_run`). The dashboard's `render_updater_status_banner` (rendered via `render_last_fetch_caption` on data pages + directly on Home/Notifications) warns when the last `price_news_update` run failed or is >20h old.
- The Twelve Data daily counter is shared across processes via `data/.twelve_data_usage`; `format_twelve_data_quota()` (utils.py) renders it as `Twelve Data today: N/800` inside `render_market_data_refresh_control` captions.
- `run_updater.py --price-news|--full|--logon|--backup` exits non-zero when a run fails — Task Scheduler will show failure.
- The daemon checks market-data freshness every 60s and runs a catch-up update when the last fetch is >4h old (covers jobs misfired while the machine slept — APScheduler drops those past `misfire_grace_time`). Catch-up retries no more than once per 30min and can't overlap a scheduled run (`_job_lock`).
- The 18:00 historical job also misfires whenever the machine sleeps through 18:00 - before 2026-11-18 it had silently not run for 6 days (no history, dividends or dead-symbol deactivation). `_historical_catch_up_if_due()` in the same 60s loop runs it when no *successful* `daily_historical` heartbeat is newer than 26h (retry at most hourly); `_historical_lock` stops cron and catch-up overlapping.
- APScheduler's own logging (misfires, job errors) is routed into `updater.log` via a stdlib→loguru handler in `setup()`.

## Dashboard conventions

- `st.rerun()` discards messages rendered in the same run - set a `session_state` flash tuple `("success"|"warning"|"info"|"error", message)` before the rerun and pop it at the top of the page on the next run (pattern in `dividends.py`).
- Destructive actions use a two-step confirm: first click stores a `*_pending` tuple in session_state and renders a warning with "Yes, ..."/"Cancel" buttons (pattern in `watchlist.py` `_render_*` remove sections).
- `st.dataframe` cells must be numeric (with `st.column_config.NumberColumn` formats) for sorting to work — never pre-format "R x" into strings. `price_column_format(*cols, signed=)` picks 2 or 4 decimals (sub-Rand prices) for a whole table.
- `render_symbol_search` only calls Yahoo once the query is 3+ chars — keep the gate; every keystroke is a new cache key = a live request.
- Adding/reactivating a stock must `_get_active_stocks.clear()` (from `app.dashboard.company`) so the Company View selector doesn't wait out its 5-min cache.

- Bullion products on the Krugerrands page are valued by `fine_oz_per_unit` from `PRODUCT_CATALOG` (fine troy ounces per item) — value = fine_oz x metal spot/oz, which prices 22k coins, .9999 gold bars and .999 silver uniformly. Holdings are stored in `krugerrand_holdings` with the fine-oz content denormalised, so catalogue edits never corrupt recorded purchases.
- Crypto holdings (`crypto_holdings` table) are keyed by **CoinGecko id** (`coin_id`, e.g. "bitcoin"), not ticker — `symbol` is display-only. `CoinGeckoCollector` needs no API key (public tier, ~30 req/min); the page caches market data 60s and search results 1h. Prices are ZAR via `vs_currency=zar`.
- **Luno sync** (`collectors/luno.py`, `services/luno_sync.py`): `crypto_holdings.source == "luno"` rows mirror Luno balances - one row per held asset (dust < 1e-8 ignored), quantity owned by the sync, rows deleted when Luno reports a zero/absent balance; a failed balance call returns None and changes nothing. Cost basis is derived from Luno transaction history (`/accounts/{id}/transactions`, needs the key's view-transactions permission) on every sync: wallet coins use average cost of coins still held (`wallet_cost` - ZAR buys add cost, sells/sends release it pro rata, fees don't); a bundle's cost is net Rand invested across ALL its accounts incl. emptied ones (`bundle_invested` - rebalances/tracking fees are TRANSFERs, not money in). Rand amounts parse from descriptions (`for R 1,000.00`, NBSP-separated) with Luno's `Approximate value` as fallback. `cost_source` is "luno" or "manual" - a manual entry is never overwritten; the page's "Use Luno history instead" clears it. A history failure keeps existing costs and surfaces `cost_error`. The bundle's Luno name (e.g. "LLC bundle", from "as part of ...") is stored as `CryptoBundle.label`. Luno rows are valued at Luno's ZAR **bid**, not last trade - thin pairs have stale last trades (DOTZAR: last R3, bid R12, zero 24h volume). Luno calls Bitcoin `XBT`; `LUNO_ASSETS` maps asset -> display symbol + CoinGecko id (for 24h change). Credentials `LUNO_API_KEY_ID`/`LUNO_API_SECRET` match the luno-mcp server's names. The page auto-syncs when the last sync is >15 min old (once per session), plus a manual button. The app calls the REST API directly - the MCP server is for AI clients, not the app. **Bundles:** Luno reports coins inside a Luno Bundle as separate `account_type == "BUNDLE"` accounts (wallets are `TRANSACTIONAL`); the sync keys rows by (coin, bundle) so wallet XRP and bundle XRP stay apart, and stores bundle coins with `bundle = "Luno Bundle"` (the API has no bundle product name). A bundle counts as ONE holding in the totals; its user-entered total cost is on `crypto_bundles` (`CryptoBundle`) and survives syncs. Assets with no ZAR pair (BNB) are priced via `XXXUSDT bid x USDTZAR bid`, then CoinGecko as a last resort.

## Dashboard/UI conventions

- **Navigation:** pages are `st.navigation` entries in `_PAGES` (`app/dashboard/home.py`) - `(title, icon, url_path, renderer(db))`, so each page has its own URL (`/portfolio`, `/settings`, ...). Icons must be real emoji: Streamlit rejects symbols like `₿` and the whole app fails to start. Add a page with one `_PAGES` line (lazy-imported via `_lazy(module, function)`) plus a `PAGE_DATA_SOURCES` entry.
- **Prices:** per-share Rand prices go through `format_price()` (text) or `price_column_format()` (NumberColumn) in `utils.py` - 4 decimals below R1, so penny stocks like SKA (R0.0085) don't render as R0.01. Totals/values stay at 2 decimals.
- **Row colours:** `style_gain_loss_row` sets an explicit dark text colour with its pastel backgrounds; without it dark mode renders white text on pale green.
- **Tables:** keep numbers numeric (format via `column_config`, not f-strings) so they sort by value and export cleanly; `render_csv_download(df, name, key)` adds the CSV button and drops `_`-prefixed helper columns.
- **Adding a stock:** use `render_symbol_search(db, key)` - stored stocks plus Yahoo's JSE listings (`YFinanceCollector.search_jse`, filtered to exchange `JNB`), so users can type a company name; an exact typed ticker still works.
- **Settings page** (`settings_status.py`) is read-only: it shows which `.env` values are set (never the values), updater heartbeats, backups and data sources, and can send a test notification. It never writes `.env`.

- **Post-rerun flash:** `st.success()`/`st.warning()` called immediately before `st.rerun()` is never seen. Store `("success"|"warning"|"info", message)` in `st.session_state` before the rerun and pop/render it near the top of the form on the next run (`watchlist_add_flash`, `portfolio_add_flash`, `dividends_fetch_flash` are the existing examples).
- **Refresh control:** pages use the shared `render_market_data_refresh_control(db, key=..., symbols=..., include_news=...)` — blocking spinner + result feedback + quota caption. Don't reintroduce fire-and-forget thread refreshes (the old Home one gave no feedback).
- **Notification types** shown by `_TYPE_ICONS` in `app/dashboard/notifications.py`: `price`, `technical`, `news`, `dividend`, `portfolio`, `ipo`, `system`. New alert types need an icon entry or they render iconless.
- **Failure vs empty:** helpers that fetch remote data should return a distinguishable error (None/flag/message), not an empty list — pages show "fetch failed" warnings only on real failures and honest "no data" info otherwise (`_get_daily_movers`, `_scan_penny_stocks` are the pattern).
- Watchlist alert preferences (`notify_price_increase`/`_decrease`/`price_change_threshold`) are set in the Watchlist Add/Edit forms; `notify_news` still has no consumer.
