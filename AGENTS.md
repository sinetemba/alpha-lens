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
- `scripts/check_stored_prices.py [--all]` - read-only stored-vs-Yahoo close check. The 2026-11-18 note that "SKA R0.0085 was verified correct" was WRONG: SKA is ~R0.85 (EasyEquities agrees) - see "JSE cents/Rand units" below. As of 2026-11-21 (after the repair) every tracked symbol agrees.
- **JSE cents/Rand units** (`collectors/jse_units.py`): Yahoo quotes .JO in ZAc but serves stretches of history in Rand (WEZ 45 -> 0.45 -> 45; ISO was R-valued until 2026-05-18; SKA/EEL's latest bars 0.85/0.03). `YFinanceCollector._to_rand` and the penny scan convert PER BAR: split the series at ~100x steps (50x-200x), the latest stretch is cents unless its last year sits below 1c (JSE minimum price) - then it's Rand. Test the 1c rule on the latest stretch's last ~250 bars only: ancient junk (RNG 0.099 in 2007, MTU 0.8 in 2006) once scaled RNG to R2.9M. Never go back to a blanket /100. The save-time x100/x0.01 correction (`_unit_factor`) refuses any result below 1c, and `normalize_closes` saves with `trust_units=True` (no anchoring on the stored bars being repaired). Every past EE "looks like cents" rescale (SKA, KBO) was wrong, so `_ee_reconcile_units` never rescales EE: a 50-200x EE-vs-stored gap keeps EE's values and raises a "Price unit conflict: SYM" system notification. Repaired 2026-11-21: backups `alphalens_20261121_232410.dump` (before) - ISO R0.20 -> R20.30, SKA R0.0085 -> R0.85, EEL R0.0003 -> R0.03, RNG/MTU re-normalised, 20 ISO listing-month junk bars and 243 sub-cent KBO bars deleted, SKA/KBO backfill buy prices corrected. `stock_prices.volume` is int32 - larger glitch volumes are stored NULL.
- `scripts/sync_easyequities.py` - headless EasyEquities holdings sync (same code path as the Portfolio page; it was `_sync_holdings.py` in the repo root). The sync itself lives in the Streamlit-free `app/services/easyequities_sync.py` (`sync_portfolio_from_easyequities`, `_sync_holding_from_easyequities`, `_ee_reconcile_units`, `_normalize_account_type`); `app/dashboard/portfolio.py` keeps a thin wrapper that stashes raw holdings for the debug expander and re-exports the helpers for tests. The updater daemon also calls it via `_sync_easyequities_if_due` (gated to ~once/day by `EE_SYNC_MIN_AGE`).

## Windows headless updater

- `scripts\run_updater.py` (via `app\services\updater.py`) is the **only** scheduler in this app - it owns all price/news/historical/dividend fetching. `main.py` (the Streamlit dashboard) does not run a background scheduler of its own; it only reads from the database and offers a manual "Refresh Prices" button. An in-process `SchedulerService` used to run inside the dashboard too (hourly, no quota/lock safeguards) - it duplicated the headless updater's work against the same shared Twelve Data quota and was removed. Do not reintroduce a scheduler inside `main.py`.
- **Recommended:** Task Scheduler via `scripts\install_updater_task.ps1` (run PowerShell as Administrator). The installed task runs with `ExecutionTimeLimit = 0` (a daemon must not hit the 3-day default kill) and `RestartCount 3`/`RestartInterval 5min` for crash recovery.
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

## Dashboard (Streamlit)

- Start it with `scripts\run_dashboard.py` (or the `AlphaLensDashboard` scheduled task from `scripts\install_dashboard_task.ps1`), **not** a bare `streamlit run main.py`. The launcher passes `STREAMLIT_SERVER_ADDRESS`/`STREAMLIT_SERVER_PORT` from `.env` (default `localhost:8501`) - a bare `streamlit run` binds every interface, and with Windows Firewall allowing Python inbound on the Public profile the dashboard (holdings, sync/backup buttons, no login) was reachable from any network the PC joined. Verified 2026-11-19: no longer reachable via the LAN IP. `.streamlit/config.toml` (tracked - the rest of `.streamlit/` stays gitignored) also sets `server.address = "localhost"`, so even a bare `streamlit run main.py` binds to 127.0.0.1 only.
- The task uses the project venv's `pythonw.exe` (no console), runs at logon **without elevation** (no admin needed to install), restarts up to 3x on crash, and writes Streamlit output to `data/logs/dashboard.log`. It previously ran on a global Python while the venv/tests/requirements pin Streamlit (1.66.1 since 2026-11-19 - bumped from 1.38 for GHSA-7p48-42j8-8846, a Windows SSRF/NTLM advisory fixed in 1.54.0) - keep it on the venv. Task Manager shows the base `Python313\pythonw.exe`; that's the venv launcher's child, loading `.venv\Lib\site-packages`.
- Restart: `schtasks /end /tn AlphaLensDashboard` then `schtasks /run /tn AlphaLensDashboard`. Remove: `scripts\uninstall_dashboard_task.ps1`.
- A bare `streamlit run main.py` failing with "Port 8501 is not available" is expected while the task is up - the app is already live on that port and the running process caches imported modules, so restart the task to pick up code changes. For a second instance: `.venv\Scripts\python.exe -m streamlit run main.py --server.port 8503` (bare `streamlit` is the global Python). The README's Troubleshooting section lists all the options (use it / second copy / swap the task for a terminal / uninstall the task).
- Local port map: 8501 AlphaLens (fixed in `.streamlit/config.toml`, so a clash fails instead of moving), 8502 fantasy-vibes (`C:\Repos\fantasy-vibes`, fixed + localhost in its `.streamlit/config.toml` since 2026-11-20). Streamlit reads `.streamlit/config.toml` from the **working directory** only: fantasy-vibes started from its `app\` folder ignores its config, takes 8501 on 0.0.0.0, and Windows lets that coexist with AlphaLens' 127.0.0.1:8501 - `localhost` (which may resolve to `::1`) then reaches fantasy-vibes instead of AlphaLens. Seen 2026-11-20. Extra copies: 8503+.

## Running the updater manually

```powershell
cd C:\Repos\AlphaLens
python scripts\run_updater.py --help
python scripts\run_updater.py --price-news
python scripts\run_updater.py --full
python scripts\run_updater.py --logon
python scripts\run_updater.py --normalize-closes   # one-shot: rewrite bars to adjusted close (backup first)
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
- Logging goes through `app/utils/logging_setup.configure_logging` (main.py + updater): it removes loguru's default DEBUG stderr sink and adds the log file, plus a console sink only when stderr is a TTY. Under the scheduled tasks stderr is redirected, so `dashboard.log` now holds only Streamlit's own output/tracebacks - app lines are in `app.log` (they used to be duplicated in both). Recurring per-item warnings are summarised per batch/feed (rescales, unparseable RSS dates); Yahoo retry notices are INFO (the caller logs the final failure).
- RSS dates: News24's capi24 feed uses `Friday Nov 20 2026 17:24:41` (no zone) - it is UTC (matches the article pages' `datePublished +00:00`, checked 2026-11-21); `RSSScraper._FALLBACK_FORMATS` parses it. Before 2026-11-21 ~25% of News24 articles were dated at fetch time; 39 older ones still are.
- The JSE index symbol `^J203.JO` must be explicitly tracked by the updater; otherwise the Home page "last market data" caption goes stale.

## SQLite to PostgreSQL migration

If a user switches `DATABASE_URL` to PostgreSQL after using SQLite, the old data in `data/stock_analyzer.db` is not copied automatically. Use:

```powershell
python scripts\migrate_sqlite_to_postgres.py
```

It truncates the Postgres tables and copies all rows from the SQLite file, preserving primary keys and resetting `id` sequences. It is idempotent.

## Upcoming JSE IPOs / new listings

Implemented 2026-11-19 per the earlier spec. Alerts the user when a company IPO
is announced so they can subscribe during the offer window (before listing day).

- **Source:** `MoneywebCollector.get_sens_announcements()` scrapes the public
  SENS page (`tools-and-data/moneyweb-sens/` — no login needed; deliberately
  not gated on `enabled`/credentials). Rows come from `div.sens-row`:
  `time[datetime]`, a `click-a-company` ticker link, and a `mny_sens` headline
  link (`NAME – headline`, or the title alone for ticker-less announcements).
- **Filter:** `is_ipo_announcement()` in `app/services/ipo.py` whitelists
  real-IPO phrases ("pre-listing statement", "intention to list", ...) after
  excluding instrument-listing noise ("new financial instrument", "listing of
  additional", interest payments). Genuine IPOs are ~5-10/year — precision
  over recall; tune the phrase lists there.
- **Flow:** `UpdaterService._update_ipo_listings()` daily 08:00 (+ `--ipo`
  one-shot) -> `update_ipo_listings(db)` stores new rows in
  `upcoming_listings` (dedup by unique `source_url`), creates `type="ipo"`
  notifications, then `_mark_newly_listed` probes `<symbol>.JO` via
  `YFinanceCollector.get_latest_bars` — first bars flip status to "listed" and
  notify "now trading". Page: `app/dashboard/upcoming_ipos.py` ("IPOs" nav,
  includes a manual "Scan SENS now" button).
- **Caveat:** early warning only — subscribing still goes through a broker
  during the offer window in the pre-listing statement.

## Review fixes 2026-11-22 (gold prices, alerts, tests, restore)

- **Gold/silver spot** (`services/metal_prices.live_metal_spots`): Yahoo
  COMEX futures x USD/ZAR first (`YFinanceCollector.get_metal_spots`), GoldAPI
  only fills a metal Yahoo missed. GoldAPI returned 403 on everything
  (2026-11-22: bad key or the 100/month quota) and the Krugerrands page, which
  never cached failures, fired ~14 sequential GoldAPI requests per rerun
  (18.8s open, 13.8s per click). Now: the page caches `_get_live_spots` 5 min
  INCLUDING failures; `GoldAPICollector` skips itself 6h after 401/403/429 and
  10 min after other errors (class-level, shared by every instance); year-end
  prices come from ONE Yahoo history download (`get_metal_year_ends_zar`,
  cached 6h) instead of 10 GoldAPI calls, Alpha Vantage as fallback. The page
  names its price source and warns when it is showing a stored price. Source
  health keys `yahoo_metals`, `goldapi`.
- **Alerts are edge-triggered** (`NotificationService.check_price_alerts` /
  `check_technical_alerts`): target/stop alert when the previous bar was on
  the other side of the level, or once when the level is already crossed and
  nothing was alerted since the watchlist item was last edited
  (`_alerted_since(title, item.updated_at)`); RSI alerts on entering the
  70/30 zone. Before, every run inside the condition re-alerted after the 20h
  dedup (SOL/NPK/DRD target alerts 4 days running). The 10 SKA alerts raised
  on the corrupted R0.0085 bars were annotated as false.
- **Tests:** `tests/test_page_smoke.py` renders all 12 pages in AppTest
  against a seeded SQLite file with non-loopback sockets refused and
  `yfinance.download/Ticker/Search` raising (yfinance uses curl_cffi, below
  Python's socket module). It stubs `streamlit.env_util.is_repl` - bare-mode
  AppTest calls inspect.stack() on every element, minutes on Windows. Don't
  monkeypatch `time.sleep` there: `module.time.sleep` IS the global one and
  Streamlit's wait loops then spin. `TEST_DATABASE_URL` (conftest) runs every
  `db_session` test on PostgreSQL, truncating tables between tests; it refuses
  a database name without "test" or equal to DATABASE_URL. CI runs both. Two
  updater tests used to call the real Luno API - mock `LunoCollector` in any
  test that runs `_update_prices_and_news`.
- **Restore drill:** `scripts/test_restore.py --target-url <scratch db>`
  restores a `.dump` (`--no-owner --no-privileges`: the dump GRANTs to the live
  `alphalens` role) and compares every table's row count with live. Verified
  2026-11-22: all 18 tables restored. Locally, a throwaway server works
  without admin rights: `initdb -D <dir> -U testuser --auth=trust` then
  `pg_ctl -D <dir> -o "-p 54329" start` (the app's role can't CREATEDB).
- **Schema changes no longer break the running dashboard:** `main.py` keys
  `_init_database_once` on a fingerprint of every model column, so a model
  edit Streamlit hot-reloads re-runs the migrations ("column ... does not
  exist" until restart, seen 2026-11-22).
- **Home movers fallback:** when Moneyweb's movers feed
  (`cache.moneyweb.co.za/mny-mover-tables.php`) fails - it returned HTTP 502
  for a few minutes on 2026-11-22 while the rest of the site was up - the
  Home page says so honestly (`_movers_failure_text`: a 5xx is Moneyweb's
  server, not the user's network) and shows the biggest last-session moves
  among the user's tracked shares from stored closes (`_tracked_movers`),
  labelled "(your shares)". Source-health key `moneyweb_movers`.
- **Company View news** uses the stored RSS articles when the store is fresh
  (<12h, `CompanyNewsService._store_is_fresh`) even if none match - the live
  5-feed scrape ran on nearly every company selection (10.9s -> 6.8s).

## SENS for held shares (suspensions, corporate actions)

Added 2026-11-22 after SGP (Sail Mining) showed as a R0 holding with no
explanation: suspended on the JSE since 2022-07-18, with a 7.2c repurchase
offer + delisting proposed 2025-12-12 and its timetable still pending.

- `services/holding_sens.refresh_holding_announcements` runs in the daily
  historical job (+ `run_updater.py --sens`): one Moneyweb company page per
  held JSE share (`MoneywebCollector.get_company_sens`, ~10 latest SENS each,
  0.5s apart, ~45s for 42 holdings) into `sens_announcements` (dedup by
  `source_url`). Not the all-company SENS page - it spans ~1 trading day, so
  a slept-through day would lose announcements. `parse_sens_rows` is shared
  by both pages.
- New rows raise `type="sens"` (📢) notifications; a symbol's first fetch
  backfills history but only alerts the last 3 days. Decision-type headlines
  (`CORPORATE_ACTION_PHRASES`: offers, delisting, odd-lot, circular, business
  rescue, liquidation...) get an "Action may be needed - " title. Routine
  AGMs are deliberately NOT actions (they flagged 28/42 holdings), and a
  later "Results of ..." announcement sharing the phrase concludes an action
  (RMH's odd-lot offer).
- `holding_statuses` -> Portfolio page: `_render_unpriced_notice` shows a
  warning per suspended holding (latest suspension notice + pending action,
  linked) and per held share with a decision-type SENS in the last 365 days;
  the Action column says "— suspended" instead of "Sell — no prices" (a
  suspended share can't be sold). Trading suspensions only: "suspension of
  operations at <mine>" (WEZ) and "suspensive conditions" are not.
- **Key facts** (`services/sens_facts.py`, rule-based): for decision-type
  announcements the page text (`MoneywebCollector.get_announcement_text`,
  trimmed by `sens_body_text`) is parsed for the salient-dates table
  (general meeting, last day to trade, voting record date, proxy deadline,
  offer opens/closes, payment), deal-sized Rand amounts (>= R1m), cents-per-
  share offer prices and irrevocable-undertaking %, max 5 by priority, stored
  " · "-joined in `sens_announcements.facts` and shown in the notification
  and Portfolio notice. Dates are matched line by line (a year from the next
  table row was once borrowed) and the circular-receipt record date is
  skipped via a next-line look-ahead. A figureless update borrows only PRICE
  facts from the earlier notice of the same action, never its dates (SGP's
  Jan update: "timetable will be revised").
- **Catch-up:** `alerted_at` marks notified rows; every open action
  (`pending_actions`: <365d, any age while suspended) not yet alerted is
  notified once with a "Still open" lead. Several open actions per holding
  are kept (APF had two circulars on 13 Nov); later notices of the same
  action supersede earlier ones, but distinct circulars all stay.
- Bond/noteholder notices put a truncated title in the `<strong>` "company"
  slot; when that text has lower-case letters, the headline is the longer of
  it and the URL slug (`_headline_from_slug`) - either can be the cut one.
- Source-health key `moneyweb_company_sens`.

## Penny stocks scan and near-52w-low alerts

- The scan lives in Streamlit-free `app/services/penny_scan.py`
  (`scan_penny_stocks`: one batched `yf.download` of the curated
  `JSE_PENNY_CANDIDATES` + `scannable_db_stocks`, 1y bars + dividends). The
  Penny Stocks page only caches it (`_scan_penny_stocks`, 30 min) and renders.
  Keep Streamlit out of `penny_scan.py` - the updater imports it.
- `JSE_PENNY_CANDIDATES` (82) = JSE equities that traded below R10 in the
  year to 2026-11-20, from probing ~290 tickers on Yahoo (202 had data). The
  scan returns `missing` (symbols with no usable history) and logs them; the
  page caption shows "N of M symbols returned data". Re-probe roughly yearly.
- `run_near_low_alerts` runs at the end of the daily historical job (so it
  gets the catch-up after sleep; a failure only logs a warning). Rows that
  are not stale and close within `PENNY_LOW_ALERT_PCT` (default 10) of their
  52w low raise `type="price"` "Near 52-week low: SYM" notifications, at most
  once per symbol per 30 days (`NEAR_LOW_COOLDOWN` - `create_notification`'s
  own dedup only spans 20h). `PENNY_LOW_ALERT_ENABLED=false` turns it off.
- `scannable_db_stocks` excludes symbols containing `. ^ _ =` (indexes,
  forex, `AV_*`/`XAU_*`, Yahoo futures like `NG=F`) - they 404 as `X.JO`.
- Each row carries `setup` (`_setup_tag`, first match wins): "Extended"
  (RSI≥70) > "Recovery · long" (≤15% off the 52w low AND stabilising -
  MACD hist >0 or RSI≥45) > "Bounce · short" (RSI<30) > "Trend · mid"
  (above SMA-20/50, positive 3m momentum) > "Weak" (below SMA-20, negative
  momentum) > "Watch". Near-low without a turn signal is deliberately NOT
  Recovery - a knife isn't a bargain. Thresholds: `SETUP_*` constants.
- `_trailing_dividend_yield` unit-checks each Yahoo dividend against its
  own day's close (>50% of the share price = cents, same rule as
  `_dividend_amount_rand`) - Yahoo mixes ZAc/ZAR per ticker, so a blanket
  /100 understates Rand-denominated yields 100x.
- Commodity stored-close units differ per pseudo-symbol (`COMMODITY_SYMBOLS`
  in `dashboard/commodities.py` carries a `unit` field): `AV_*` metals are
  ZAR/gram (Metals.dev `unit=g`), `MD_*` base metals (aluminum, nickel,
  zinc, lead) are ZAR/**metric tonne** - Metals.dev applies `unit=g` to
  every metal, so base-metal rates are x1e6 at save time. LBMA/MCX/IBJA
  symbols in the response are deliberately untracked. The Alpha Vantage
  backfill serves native USD units (gold/silver per troy oz, copper per
  metric tonne) - `_save_historical_prices` converts `USD x ZAR=X /
  _AV_GRAMS_PER_UNIT` at save time; the `commodity_av_units_v1`
  schema_meta repair deleted the legacy unit-mixed bars.
- Energy/Agriculture/Livestock on the Commodities page come from Yahoo
  front-month futures (`services/commodity_futures.py`, `FUTURES`: CL=F,
  BZ=F, NG=F, RB=F, HO=F, ZW=F, ZC=F, ZS=F, KC=F, SB=F, CC=F, CT=F, LE=F,
  HE=F) stored as `YF_*` pseudo-symbols in ZAR per quote unit, converted at
  each day's `ZAR=X`. Grains/softs/livestock quote in **USX (US cents)** -
  the per-contract scale is 0.01. One batched keyless request (1y backfill,
  then 1mo incremental); refreshed by the page (auto once per session when
  stale + Refresh button) and the daily historical job. The page renders
  one table and one chart tab per `CATEGORY_ORDER` entry; copper is stored
  per gram but displayed per tonne (`_DISPLAY_PER_TONNE`).
- SA grains (`services/safex_prices.py`, `collectors/safex.py`): SAFEX white
  maize / yellow maize / wheat as `SFX_WMAZ`/`SFX_YMAZ`/`SFX_WEAT` in R/tonne,
  scraped from Grain SA's public SAFEX Feeds page (server-rendered table; the
  JSE site 403s scripts). "The price" = front contract: nearest month AFTER
  the trading day's month (the delivery-month contract goes thin). The "SA
  Grains (SAFEX)" table shows a `vs CBOT` gap against `YF_CORN`/`YF_WHEAT`
  converted at 39.3679 / 36.7437 bushels per tonne (white maize vs CBOT corn
  - no white-maize CBOT contract). Refreshed with the futures (page + daily
  historical job). No history backfill: SAGIS publishes yearly .xls files
  only after year-end (2025 = latest), useless for the 90-day charts, so the
  series accumulates from 2026-11-20. A Grain SA redesign makes
  `get_contracts()` return None and logs "layout changed?".

## Cross-DB SQL expressions

The `strftime('%Y-%m-%d', timestamp)` function is SQLite-only. For PostgreSQL, use `func.date_trunc('day', timestamp)`. The `app/dashboard/utils.py` helper `day_of(db, col)` selects the right one from the session's engine dialect (not `settings.database_url`, so SQLite tests work against a Postgres-configured `.env`).

## Data-write conventions

- Zero/negative OHLC values are treated as missing (`_apply_price_values`) - Yahoo history uses `auto_adjust=True` and has no `Adj Close`, so `adjusted_close` falls back to `close`. 13,365 legacy `adjusted_close = 0.0` rows were repaired 2026-11-18.
- Every Yahoo path uses `auto_adjust=True` (`get_latest_bars` included since 2026-11-21), so `close_price` is uniformly adjusted close - latest-bar and history paths no longer mix raw/adjusted around ex-dividend days. `run_updater.py --normalize-closes` (`DataService.normalize_closes`) rewrites stored bars from adjusted history and recomputes indicators; it ran once on 2026-11-21 (backup taken first). Re-run it if a symbol's older history was re-ingested raw.
- Daily bars are keyed by naive exchange-local midnight (`YYYY-MM-DD 00:00`), not UTC instants. Re-fetches overwrite the same bar in place — never insert a duplicate row for the same `(symbol, timestamp)`; that pair has a unique index.
- All `DateTime` columns are naive and store UTC. Use `utcnow_naive()` from `app/models/base.py` for defaults/writes — never write `datetime.now(timezone.utc)` (aware) into a naive column; SQLite would store a `+00:00` suffix and break ordering.
- Save helpers in `data_service.py` (`_save_price_from_quote`, `_save_recent_bars`) return `bool` and roll back on error. `_save_recent_bars` writes every bar in the fetched Yahoo frame (gap-filling missed days), not just the latest. `update_stock_prices`/`update_dividends` **require** an explicit symbol list (no all-stocks fallback — that was a quota trap); they batch Twelve Data quotes 10-per-request and Yahoo fallbacks via `yf.download`. Batched quotes must carry `mic_code == XJSE` per result — same strict check as `get_quote`.
- `_get_tracked_symbols` (updater) returns watchlist + JSE-eligible portfolio symbols **intersected with `Stock.is_active`** — a deactivated stock is not fetched even if still on a watchlist. Deactivation is the bad-ticker/delisting kill switch: the add paths set it automatically when a symbol has no backfill and no stored prices, re-adding reactivates, and Company View's "Manage tracked stocks" expander toggles it manually.
- `update_historical_data` is incremental: once a backfill exists it fetches only ~the last stored bar + 7-day overlap, not the full window. The daily job calls `update_historical_data_batch(symbols)` — same per-symbol logic (single-symbol version remains for the dashboard add paths) but one grouped stats query + one batched `yf.download` (`get_history_batch`) for all Yahoo fallbacks. `_update_technical_indicators(symbol, full=?, tail=N)` — `full=True` recomputes all history (backfills); `tail` controls how many trailing bars get persisted indicator values on incremental runs (pass `new_bars + 1`).
- Implausible closes (>3x vs previous stored close, `MAX_CLOSE_RATIO`) are cross-checked against the *other* provider before rejection (`_close_corroborated`): corroborated moves (crashes, splits/consolidations) are accepted, uncorroborated ones rejected, and both raise a `type="system"` in-app Notification. Corroboration applies to both single-bar and bulk-save paths.
- JSE dividend amounts may be cents or Rand per ticker on BOTH providers; `update_dividends` rescales amounts >50% of the contemporaneous share price (within 730 days of ex-date) to Rand for Yahoo and Twelve Data alike - the manual add on the Dividends page applies the same check. Dividend dedup is per (calendar day, amount) — a special dividend can share an ex-date with an interim, so the unique index is on all three columns. `update_dividends(symbols, freshness_days=7)` skips symbols whose `Stock.last_dividend_fetch` is inside the window (stamped even for non-payers - a Stock row is created so the stamp sticks); the Dividends page button passes `freshness_days=0` to force. `scripts/fix_dividend_amounts.py --apply` already repaired legacy duplicates/cent amounts once; don't re-run it blindly.
- EasyEquities amounts: `_ee_reconcile_units` (`services/easyequities_sync.py`) checks the implied price vs the stored JSE close on JSE-tracked accounts and, on a ~100x gap, keeps EE's values and raises a "Price unit conflict" notification (it no longer rescales EE - see "JSE cents/Rand units") — the parser's currency code is kept on the holding dict (`"currency"`); non-ZAR amounts on JSE accounts are logged and skipped, not trusted. Non-JSE accounts' aggregate values arrive in USD and are converted at `ZAR=X` (`YFinanceCollector.get_usd_zar`, 1h in-process cache) before the Rand-denominated `purchase_price`/`current_price`/totals are computed — when the rate is unavailable the values are left at 0 rather than storing dollars-as-Rand.
- Alert Notification rows are created on every check run regardless of `notification_enabled` (the dashboard page is an in-app channel). With delivery disabled they're stamped `is_sent=True`/`delivery_channel="in-app"` at creation so they never queue as a stale backlog; `send_pending_notifications` only attempts rows <24h old (newest first, attempts capped at 5), so a backlog can't flood Apprise when delivery is re-enabled later.
- `get_latest_and_previous_prices` (`services/price_queries.py`) scopes its window CTE to `_PREV_DAY_WINDOW` (10 days) back from each symbol's own latest bar — don't widen it without a reason; a suspended stock must not present a weeks-old close as "yesterday's" (a fake daily move). Symbols suspended longer get no previous-day snapshot.
- Portfolio value history: `portfolio_snapshots` holds one row per portfolio per SAST day (unique), upserted by `services/portfolio_valuation.update_portfolio_totals` - called by the Portfolio page and, via `revalue_portfolio`, after every updater price run, so the series fills in even when the dashboard isn't opened. Revaluation lives in that Streamlit-free module; the page's `_update_holding_values` is a thin wrapper passing its cached price lookup.
- **Net worth** (`services/net_worth.py`): JSE + crypto + bullion. Shared by Home's Net Worth metric (cached 5 min) and the updater: after every price run `_snapshot_portfolio` syncs Luno + EasyEquities (`_sync_easyequities_if_due`, ~once/day), revalues the portfolio and writes `crypto_value`/`bullion_value` into the day's snapshot (NULL = not measured, never zero - a JSE-only revaluation from the Portfolio page never wipes them). Crypto uses `crypto_price` (Luno bid for Luno rows, CoinGecko otherwise); bullion uses Yahoo `GC=F`/`SI=F` x `ZAR=X` (`YFinanceCollector.get_metal_spots_zar`) so the headless updater never spends GoldAPI's 100/month - matches GoldAPI to ~0.01%. Yahoo-sourced spots are also flushed to `XAU_ZAR`/`XAG_ZAR` day-midnight bars (`_persist_yahoo_metal_spots`) so the stored series stays fresh on days nobody opens the Krugerrands page. Don't use `AV_GOLD`/`AV_SILVER` for valuation: the Alpha Vantage series lag badly (AV_GOLD $2,071 vs spot $3,726 on 2026-11-18). Home values JSE holdings at the latest stored close; the Portfolio page shows EasyEquities' last synced prices - the two differ by intraday moves, by design.
- `_maybe_deactivate_dead_symbol` also deactivates a stock that has NEVER priced once its Stock row is >45 days old (SGP: suspended, R0 on EasyEquities, unknown to Yahoo) - previously it errored every run forever. The Portfolio page names JSE holdings with no bar in 7 days (`_render_unpriced_notice`) so frozen EasyEquities values aren't mistaken for live prices.
- News24's old `Services/Site/Feeds/...` RSS URL returned 404 from 2026-08-07; the working feed is `https://feeds.capi24.com/v1/Search/articles/news24/Business/rss`.
- `_ensure_stock` backfills sector/industry/market_cap/shares/description/website from Yahoo `get_info` once per stock — gated on `details_fetched_at` (stamped on the first successful profile fetch, even a sector-less one; NULL after a failed fetch so transient outages retry). Never call `get_info` in a save path without checking it.
- **Source health** (`services/source_health.py`): every external fetch writes a `updater_runs` heartbeat `source:<key>` (yahoo_prices, yahoo_futures, safex, metals_dev, moneyweb_login, moneyweb_sens, easyequities, luno, `rss:<host>` per feed) via `record_source` (commits; best-effort; call after the caller's commit). Settings > "Data source health" lists them; `check_source_health` (end of each price+news run) raises a "Data source failing: ..." system notification when a source has failed every attempt for 48h+ (dedup 20h = daily reminder) and prunes rows >30d. Silence is not failure - an unfetched source never alerts. New sources: add a `SOURCES` label and a `record_source` call in the fetch function.
- One-time data repairs in `init_db.py` are gated on `schema_meta` keys (e.g. `normalize_bar_timestamps_v1`) — checked via a PK lookup so they don't scan the table on every dashboard/daemon startup. New repair passes should add their own key, not re-run unconditionally.
- RSI/ATR use Wilder smoothing (`ewm(alpha=1/n)`). All stored history was recomputed (`full=True`) on 2026-11-18, so it is uniformly Wilder. Commodity snapshot series (`AV_*`, `XAU_*`) are close-only, so high/low indicators (ADX etc.) log a calculation error for them - expected.
- USD-account portfolio holdings (`is_jse_tracked_account` == False) are never priced/valued from JSE bars — `_update_holding_values` and Home's metrics only apply stored JSE closes to JSE-tracked accounts; EE-synced USD values pass through untouched. Holdings priced from `account_type` appear in Rand totals everywhere.
- `PortfolioHolding.account_type == "Manual"` marks dashboard-added holdings — EasyEquities reconciliation deletes any holding under an EE-reported account type that EE stops reporting, so manual rows must never carry a real EE account name or the next sync deletes them.
- Realized P&L lives in `portfolio_transactions` (`PortfolioTransaction`, written by `services/transactions.record_transaction`/`record_sale`): buys are logged by `_add_holding` and by EE sync when a position first appears; sells by `_remove_holding` and by EE reconciliation when a position disappears (exit price = last EE price). Rows are flush-only inside the caller's commit. The Portfolio page's "Transaction history & realized P&L" expander renders it; `transacted_at` is sync/write time — the real trade date isn't exposed by EE.
- When a JSE price lookup finds no stored bar, holdings keep their last-known (EE-synced) values — never null fields on a missing fetch.
- The updater writes a heartbeat row to the `updater_runs` table on every job completion (`_record_run`). The dashboard's `render_updater_status_banner` (rendered via `render_last_fetch_caption` on data pages + directly on Home/Notifications) warns when the last `price_news_update` run failed or is >20h old.
- The Twelve Data daily counter is shared across processes via `data/.twelve_data_usage`; `format_twelve_data_quota()` (utils.py) renders it as `Twelve Data today: N/800` inside `render_market_data_refresh_control` captions.
- `run_updater.py --price-news|--full|--logon|--backup|--ipo` exits non-zero when a run fails — Task Scheduler will show failure.
- Cross-process job lock (`app/services/process_lock.py`, msvcrt/fcntl on `data/.updater.lock`) is taken **per job, not per process** - price+news, historical, IPO scan, weekly cleanup (`@_with_process_lock` in updater.py) and the dashboard's Refresh Prices button. A process-life lock (an earlier draft) made the always-on daemon lock out the `--logon` unlock refresh and manual `--full` runs all session. Re-entrant per owning thread (`_owner`/`_gate`) - the scheduler runs different jobs on different threads and they must NOT share the depth counter; non-owner threads wait up to `timeout` then fail. Daemon jobs wait up to 10 min for another job, one-shots up to 30 min then exit 1; `--logon` never waits (skips, exit 0 - the other process is refreshing anyway); the dashboard waits 5s then says the updater is mid-fetch. `--backup` is exempt (read-only). Per-machine - can't stop a daemon on another host sharing one Postgres. Tests point `process_lock.JOB_LOCK` at a temp file (autouse fixture in conftest).
- IPO scan statuses: `announced` -> `listed` (bars only from the announcement day on) / `existing` (ticker already traded before the announcement - a listed company's capital raise; no alerts) / `expired` (no listing within 180 days; no longer probed). "Private placement" is neither an include nor an exclude phrase - IPOs are often done by way of one; the already-trading check handles listed companies.
- Transaction log: EE sync logs quantity changes (top-up = buy of the delta at the lot cost implied by EE's average cost; reduction = partial sell at EE's price), pairs a position vanishing from one account and appearing in another as `xfer_out`/`xfer_in` (no P&L), and only reconciles accounts that reported at least one holding (an account seen with only zero-share rows keeps its holdings + raises a system notification). `backfill_opening_buys_v1` (schema_meta) gave pre-existing holdings an opening buy. Manual Remove asks "Sold it" (price/date) vs "Entered by mistake" (deletes the holding's manual buys, no sale); single transactions can be deleted from the history expander. Realized P&L is labelled approximate (average cost, EE events dated at sync time).
- The daemon checks market-data freshness every 60s and runs a catch-up update when the last fetch is >4h old (covers jobs misfired while the machine slept — APScheduler drops those past `misfire_grace_time`). Catch-up retries no more than once per 30min and can't overlap a scheduled run (`_job_lock`).
- The 18:00 historical job also misfires whenever the machine sleeps through 18:00 - before 2026-11-18 it had silently not run for 6 days (no history, dividends or dead-symbol deactivation). `_historical_catch_up_if_due()` in the same 60s loop runs it when no *successful* `daily_historical` heartbeat is newer than 26h (retry at most hourly); `_historical_lock` stops cron and catch-up overlapping.
- APScheduler's own logging (misfires, job errors) is routed into `updater.log` via a stdlib→loguru handler in `setup()`.

## Dashboard conventions

- `st.rerun()` discards messages rendered in the same run - set a `session_state` flash tuple `("success"|"warning"|"info"|"error", message)` before the rerun and pop it at the top of the page on the next run (pattern in `dividends.py`).
- Destructive actions use a two-step confirm: first click stores a `*_pending` tuple in session_state and renders a warning with "Yes, ..."/"Cancel" buttons (pattern in `watchlist.py` `_render_*` remove sections).
- `st.dataframe` cells must be numeric (with `st.column_config.NumberColumn` formats) for sorting to work — never pre-format "R x" into strings. Per-share price columns use `style_price_columns(styler, df, reference, price_cols=, signed_cols=)` (utils.py): decimals per ROW on a pandas Styler - 2 for rows whose reference price is R1+, up to 4 with trailing zeros trimmed below R1 (R 0.0085, R 0.62) - and leave `format` OFF those NumberColumns so Streamlit shows the Styler text (it only does when `format` is unset; values stay numeric and sortable). `price_column_format` is column-wide (one penny stock put every row on R 210.3800) - no page uses it any more.
- `render_symbol_search` only calls Yahoo once the query is 3+ chars — keep the gate; every keystroke is a new cache key = a live request.
- Adding/reactivating a stock must `_get_active_stocks.clear()` (from `app.dashboard.company`) so the Company View selector doesn't wait out its 5-min cache.

- Bullion products on the Krugerrands page are valued by `fine_oz_per_unit` from `PRODUCT_CATALOG` (fine troy ounces per item) — value = fine_oz x metal spot/oz, which prices 22k coins, .9999 gold bars and .999 silver uniformly. Holdings are stored in `krugerrand_holdings` with the fine-oz content denormalised, so catalogue edits never corrupt recorded purchases.
- Crypto holdings (`crypto_holdings` table) are keyed by **CoinGecko id** (`coin_id`, e.g. "bitcoin"), not ticker — `symbol` is display-only. `CoinGeckoCollector` needs no API key (public tier, ~30 req/min); the page caches market data 60s and search results 1h. Prices are ZAR via `vs_currency=zar`.
- **Luno sync** (`collectors/luno.py`, `services/luno_sync.py`): `crypto_holdings.source == "luno"` rows mirror Luno balances - one row per held asset (dust < 1e-8 ignored), quantity owned by the sync, rows deleted when Luno reports a zero/absent balance; a failed balance call returns None and changes nothing. Cost basis is derived from Luno transaction history (`/accounts/{id}/transactions`, needs the key's view-transactions permission) on every sync: wallet coins use average cost of coins still held (`wallet_cost` - ZAR buys add cost, sells/sends release it pro rata, fees don't); a bundle's cost is net Rand invested across ALL its accounts incl. emptied ones (`bundle_invested` - rebalances/tracking fees are TRANSFERs, not money in). Rand amounts parse from descriptions (`for R 1,000.00`, NBSP-separated) with Luno's `Approximate value` as fallback. `cost_source` is "luno" or "manual" - a manual entry is never overwritten; the page's "Use Luno history instead" clears it. A history failure keeps existing costs and surfaces `cost_error`. The bundle's Luno name (e.g. "LLC bundle", from "as part of ...") is stored as `CryptoBundle.label`. Luno rows are valued at Luno's ZAR **bid**, not last trade - thin pairs have stale last trades (DOTZAR: last R3, bid R12, zero 24h volume). Luno calls Bitcoin `XBT`; `LUNO_ASSETS` maps asset -> display symbol + CoinGecko id (for 24h change). Credentials `LUNO_API_KEY_ID`/`LUNO_API_SECRET` match the luno-mcp server's names. The page auto-syncs when the last sync is >15 min old (once per session), plus a manual button. The app calls the REST API directly - the MCP server is for AI clients, not the app. **Bundles:** Luno reports coins inside a Luno Bundle as separate `account_type == "BUNDLE"` accounts (wallets are `TRANSACTIONAL`); the sync keys rows by (coin, bundle) so wallet XRP and bundle XRP stay apart, and stores bundle coins with `bundle = "Luno Bundle"` (the API has no bundle product name). A bundle counts as ONE holding in the totals; its user-entered total cost is on `crypto_bundles` (`CryptoBundle`) and survives syncs. Assets with no ZAR pair (BNB) are priced via `XXXUSDT bid x USDTZAR bid`, then CoinGecko as a last resort.

## Dashboard/UI conventions

- **Navigation:** pages are `st.navigation` entries in `_PAGES` (`app/dashboard/home.py`) - `(title, icon, url_path, renderer(db))`, so each page has its own URL (`/portfolio`, `/settings`, ...). Icons must be real emoji: Streamlit rejects symbols like `₿` and the whole app fails to start. Add a page with one `_PAGES` line (lazy-imported via `_lazy(module, function)`) plus a `PAGE_DATA_SOURCES` entry.
- **Prices:** per-share Rand prices go through `format_price()` (text) or `style_price_columns()` (tables) in `utils.py` - 4 decimals below R1, so penny stocks like EEL (R0.03) or a R0.0085 price don't render as R0.01. Totals/values stay at 2 decimals.
- **Row colours:** `style_gain_loss_row` sets an explicit dark text colour with its pastel backgrounds; without it dark mode renders white text on pale green. It reads the visible `"Gain/Loss"` column (the `_gain_loss` helper column was removed) — callers that let the user hide that column pass the value explicitly (see `portfolio.py`).
- **Tables:** keep numbers numeric (format via `column_config`, not f-strings) so they sort by value and export cleanly; `render_csv_download(df, name, key)` adds the CSV button and drops `_`-prefixed helper columns.
- **Adding a stock:** use `render_symbol_search(db, key)` - stored stocks plus Yahoo's JSE listings (`YFinanceCollector.search_jse`, filtered to exchange `JNB`), so users can type a company name; an exact typed ticker still works.
- **Settings page** (`settings_status.py`) is read-only: it shows which `.env` values are set (never the values), updater heartbeats, backups and data sources, and can send a test notification. It never writes `.env`.

- **Post-rerun flash:** `st.success()`/`st.warning()` called immediately before `st.rerun()` is never seen. Store `("success"|"warning"|"info", message)` in `st.session_state` before the rerun and pop/render it near the top of the form on the next run (`watchlist_add_flash`, `portfolio_add_flash`, `dividends_fetch_flash` are the existing examples).
- **Refresh control:** pages use the shared `render_market_data_refresh_control(db, key=..., symbols=..., include_news=...)` — blocking spinner + result feedback + quota caption. Don't reintroduce fire-and-forget thread refreshes (the old Home one gave no feedback).
- **Notification types** shown by `_TYPE_ICONS` in `app/dashboard/notifications.py`: `price`, `technical`, `news`, `dividend`, `portfolio`, `ipo`, `system`. New alert types need an icon entry or they render iconless.
- **Failure vs empty:** helpers that fetch remote data should return a distinguishable error (None/flag/message), not an empty list — pages show "fetch failed" warnings only on real failures and honest "no data" info otherwise (`_get_daily_movers`, `_scan_penny_stocks` are the pattern).
- **Portfolio "Action" column** (`_holding_action` + `_holding_price_stats` in `portfolio.py`): rule-based Sell/Trim/Buy/Hold per holding — watchlist stop→"Sell — stop hit", watchlist target + profit→"Trim — target hit", loser >15% down in a downtrend→"Sell — still falling", loser turning (above SMA-20 + positive 3m momentum)→"Buy — recovering", winner in uptrend→"Buy — uptrend", JSE holding with no stored prices→"Sell — no prices", non-JSE→"—". Stats come from one grouped `StockPrice` query (370d); a loss alone is never a sell signal.
- Watchlist alert preferences (`notify_price_increase`/`_decrease`/`price_change_threshold`/`notify_news`) are set in the Watchlist Add/Edit forms. `notify_news` is consumed by `NotificationService.check_news_alerts` — articles whose extracted `stock_symbol` matches a subscribed watchlist item raise `type="news"` alerts (scans articles created within `ALERT_DEDUP_WINDOW` so each alerts once).
