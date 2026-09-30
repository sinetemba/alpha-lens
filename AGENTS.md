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

## Windows headless updater

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

- **Twelve Data** free tier: 8 requests/minute, 800/day.
- The app throttles automatically and falls back to **Yahoo Finance** for prices, history, and dividends.
- Many JSE symbols are not on the free Twelve Data plan, so expect to see 400/403/wrong-exchange warnings and Yahoo fallback logs.

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

## Cross-DB SQL expressions

The `strftime('%Y-%m-%d', timestamp)` function is SQLite-only. For PostgreSQL, use `func.date_trunc('day', timestamp)`. The `app/dashboard/utils.py` helper `day_of(col)` selects the right one based on `settings.database_url`.
