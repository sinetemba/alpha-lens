# AlphaLens

An open-source, Python-based web application for tracking investments on and
around the Johannesburg Stock Exchange (JSE) - stocks, dividends, IPOs and a
penny-stock screener, plus portfolio sync, crypto and bullion in one
net-worth view.

## Features

- **Market Data**: JSE stock prices, history and dividends (Yahoo Finance; Twelve Data when enabled)
- **Watchlist Management**: target/stop-loss levels and price alerts, which also become your holdings' exit rules
- **Portfolio Tracking**: EasyEquities sync, per-holding gain/loss, and a rule-based **Action** column suggesting hold/buy/sell/trim from trend stats and your own exits
- **Net Worth**: JSE portfolio, crypto and bullion in one view
- **Penny Stocks Screener**: JSE shares under R5 scored on liquidity, trend and dividends, with per-row setup tags (recovery/bounce/trend) and stars on shares you already hold
- **IPO Tracker**: Moneyweb SENS announcements filtered to real IPOs, with alerts during the offer window
- **Commodities & Krugerrands**: Metals.dev precious and base metal spots in ZAR, plus Krugerrand prices via GoldAPI
- **Crypto**: Luno/CoinGecko prices and holdings
- **News Aggregation**: Financial news from multiple South African sources
- **Technical Analysis**: RSI, MACD, Bollinger Bands and more
- **Automated Data Collection**: Scheduled updates via the headless updater (3x/day prices+news, daily full, logon refresh)
- **Notifications**: Alerts for price changes, news, technical signals, near-52w lows and new listings
- **Interactive Charts**: Visualizations using Plotly

## Tech Stack

- **Frontend**: Streamlit, Plotly, AgGrid
- **Backend**: Python 3.13+
- **Database**: SQLite (development), PostgreSQL (production)
- **ORM**: SQLAlchemy
- **Scheduler**: APScheduler
- **Data Sources**: Twelve Data API, Yahoo Finance, Moneyweb, RSS feeds, EasyEquities
- **Notifications**: Apprise (Email, ntfy, Discord, Telegram)

## Project Structure

```
stock-analyzer/
├── app/
│   ├── analytics/          # Technical analysis indicators
│   ├── collectors/         # Data collectors (Twelve Data, Yahoo Finance)
│   ├── config/             # Configuration management
│   ├── dashboard/          # Streamlit dashboard pages
│   ├── database/           # Database initialization
│   ├── models/             # SQLAlchemy models
│   ├── notifications/      # Notification service
│   ├── scrapers/           # News scrapers (RSS)
│   └── services/           # Business logic (scheduler, data service)
├── data/                   # Data directory (database, cache, logs)
├── Dockerfile              # Docker image
├── docker-compose.yml      # Docker Compose configuration
├── tests/                  # Test files
├── main.py                 # Main application entry point
├── requirements.txt        # Python dependencies
└── .env.example           # Environment variables template
```

## Installation

### Prerequisites

- Python 3.13 or higher
- pip (Python package manager)
- Git

### Step 1: Clone the Repository

```bash
git clone https://github.com/yourusername/stock-analyzer.git
cd stock-analyzer
```

### Step 2: Create Virtual Environment

```bash
python -m venv venv

# On Windows
venv\Scripts\activate

# On macOS/Linux
source venv/bin/activate
```

### Step 3: Install Dependencies

```bash
pip install -r requirements.txt
```

### Step 4: Configure Environment Variables

Copy the example environment file and configure your settings:

```bash
copy .env.example .env
```

Edit `.env` and configure the following:

**Required for full functionality:**
- `TWELVE_DATA_API_KEY`: Get your free API key from [Twelve Data](https://twelvedata.com/)
- `DATABASE_URL`: SQLite database path (default: `sqlite:///./data/stock_analyzer.db`)

**Optional:**
- `EASYEQUITIES_USERNAME`/`EASYEQUITIES_PASSWORD`: live portfolio sync
- `MONEYWEB_USERNAME`/`MONEYWEB_PASSWORD`: top movers + Dividend Watch
- `LUNO_API_KEY_ID`/`LUNO_API_SECRET`: crypto holdings (read-only key)
- `GOLD_API_KEY`, `METALS_DEV_API_KEY`, `ALPHA_VANTAGE_API_KEY`: bullion/commodity prices
- `NOTIFICATION_ENABLED`: Set to `true` to enable notifications
- Notification channel settings (email, ntfy, Discord, Telegram)

### Step 5: Initialize Database

```bash
python -c "from app.database.init_db import init_database; init_database()"
```

This will create the database tables and seed it with popular JSE stocks.

## Running the Application

### Development Mode

```bash
streamlit run main.py
```

The application will be available at `http://localhost:8501`

> **Note:** if the `AlphaLensDashboard` scheduled task is installed it already
> runs the app at logon, so `streamlit run` fails with
> `Port 8501 is not available` - just open http://localhost:8501 instead. To
> pick up code changes restart the task
> (`schtasks /end /tn AlphaLensDashboard` then `schtasks /run /tn
> AlphaLensDashboard`); the running process caches imported modules. For a
> second dev instance use
> `.venv\Scripts\python.exe -m streamlit run main.py --server.port 8503`
> (8502 is taken by another local Streamlit app, fantasy-vibes). See
> [Troubleshooting](#port-8501-is-not-available) for all the options.

### Production Mode

For production deployment, consider using:

1. **Docker** (recommended)
2. **Gunicorn** with Streamlit
3. **Cloud platforms** (Streamlit Cloud, AWS, GCP, Azure)

See the Docker section below for containerized deployment.

## Usage

### Dashboard Navigation

The application has twelve main sections:

1. **Home**: JSE index, portfolio value, net worth, daily gain, Moneyweb top movers, and recent news
2. **Watchlist**: Manage your stock watchlist with target/stop-loss levels and alerts
3. **Portfolio**: Track holdings and performance, with an Action column suggesting hold/buy/sell per holding
4. **Dividends**: Track dividend history and payments
5. **IPOs**: Upcoming JSE listings detected from Moneyweb SENS announcements
6. **Notifications**: View price/technical alerts raised by the updater (mark read/unread)
7. **Company View**: Detailed information about specific stocks
8. **Penny Stocks**: Screener for JSE shares trading below R5
9. **Commodities**: Precious and base metal spot prices (gold, silver, platinum, palladium, copper, aluminum, nickel, zinc, lead)
10. **Krugerrands**: Track Krugerrand prices
11. **Crypto**: Track crypto prices and Luno holdings
12. **Settings**: Data-source status, updater heartbeats and notification test

Every page shows a stale-data banner when the headless updater's last
`updater_runs` heartbeat is >20h old or its last run failed.

### Adding Stocks to Watchlist

1. Navigate to the **Watchlist** page
2. Expand the "Add Stock to Watchlist" section
3. Enter the stock symbol (e.g., NPN, CFR, AGL)
4. Set optional parameters (target price, stop loss, purchase price)
5. Click "Add to Watchlist"

### Adding Portfolio Holdings

1. Navigate to the **Portfolio** page
2. Expand the "Add Holding" section
3. Enter the stock symbol, quantity, purchase price, and purchase date
4. Click "Add Holding"

### Viewing Company Details

1. Navigate to the **Company View** page
2. Select a stock from the dropdown
3. View price charts, historical data, and company news

## Windows Headless Updater (Scheduled Task / Service)

The dashboard itself does not run any background scheduler - it only reads from the database and offers a manual "Refresh Prices" button. All price, news, historical, and dividend fetching happens in the headless updater below, so install it to keep data fresh.

### Default schedule (Africa/Johannesburg)

- **09:00, 13:00, 17:00** – price and news update for tracked watchlist + portfolio (Twelve Data, falling back to Yahoo Finance)
- **18:00** – full daily update (historical prices + dividends)
- **Sunday 02:00** – remove news articles older than 90 days
- **On user logon and workstation unlock** – lightweight price + news refresh (debounced to avoid rapid lock/unlock loops)

### Recommended: Windows Task Scheduler

Make sure your virtual environment exists, then open PowerShell as Administrator and run:

```powershell
.\scripts\install_updater_task.ps1
```

The script prefers the Python interpreter in `.venv\Scripts\python.exe` or `venv\Scripts\python.exe` and falls back to the first `python` in `PATH`.

This creates the `AlphaLensUpdater` scheduled task. Remove it with `.\scripts\uninstall_updater_task.ps1`.

### Alternative: Windows Service

If the machine must update before anyone logs on, install the pywin32-based Windows Service using your venv interpreter:

```powershell
pip install pywin32
python scripts\windows_service.py --install
python scripts\windows_service.py --start
```

Use `python scripts\windows_service.py --stop` or `--remove` to manage it.

### API rate limits

The updater reuses the existing Twelve Data throttling and adds a pre-flight quota estimate. If the daily quota is too low for the planned run, it logs the shortfall and falls back to Yahoo Finance.

## Production Database: PostgreSQL

SQLite is perfect for development and single-user desktop use. If you are running the headless updater alongside the Streamlit dashboard, or if multiple users will access the app, switch to PostgreSQL.

### 1. Install PostgreSQL

On Windows, download and install from [postgresql.org/download](https://www.postgresql.org/download/windows/). On macOS/Linux, use your package manager or Docker.

### 2. Create the database and user

Run the included SQL bootstrap as the `postgres` superuser:

```powershell
psql -U postgres -f .\scripts\setup_postgres.sql
```

### 3. Update your `.env`

```env
DATABASE_URL=postgresql://alphalens:changeme@localhost:5432/alphalens
```

### 4. Install the driver and create the tables

```bash
pip install -r requirements.txt
python -c "from app.database.init_db import init_database; init_database()"
```

The app will create the tables automatically. No manual schema import is required.

## Database Migrations

Schema changes are applied automatically on startup by
`app/database/init_db.py` (`create_tables()` for new tables plus
`_migrate_schema()` for column/index additions and backfills) - there is
nothing to run manually.

To add a column or index to an existing table, add an idempotent step to
`_migrate_schema()` (inspect first, then `ALTER TABLE` / `CREATE INDEX`) -
`create_all` never alters tables that already exist. Use SQL that works on
both SQLite and PostgreSQL (e.g. `TIMESTAMP`, not `DATETIME`). There is no
Alembic; it was scaffolded but never used and has been removed.

## Backups

The updater daemon backs up the database whenever the newest backup is more
than `BACKUP_INTERVAL_DAYS` (default 7) old, keeping the newest `BACKUP_KEEP`
(default 8) files in `BACKUP_DIR` (default `data/backups/`, gitignored).
PostgreSQL uses `pg_dump -Fc` (found on `PATH` or under
`C:\Program Files\PostgreSQL\*\bin`, or set `PG_DUMP_PATH`); SQLite uses an
online file copy.

```powershell
python scripts\run_updater.py --backup     # back up now

# Restore a PostgreSQL dump (stop the dashboard and updater first)
& "C:\Program Files\PostgreSQL\18\bin\pg_restore.exe" --clean --if-exists -h localhost -U <user> -d <db> data\backups\alphalens_<stamp>.dump
```

**Test that a backup really restores** (the automatic check only proves the
archive is readable). Restore it into a separate scratch database and compare
every table with the live one - the target's name must contain `test`, it is
overwritten:

```powershell
python scripts\test_restore.py --target-url postgresql://<user>:<pass>@localhost:5432/alphalens_restore_test
```

Do this every few months and before relying on a backup for a repair.

`python scripts\check_stored_prices.py [--all]` is a read-only check of stored
closes against Yahoo (unit mismatches, wrong instruments, stale feeds).

## Data Sources

### Primary: Twelve Data API

- Daily prices
- Intraday prices
- Historical candles
- Technical indicators

Get a free API key at: https://twelvedata.com/

### Backup: Yahoo Finance (yfinance)

- Historical prices
- Dividends and splits
- Company metadata
- Financial statements

### Top Movers (Moneyweb)

- Daily winners and losers
- Requires Moneyweb credentials (`MONEYWEB_USERNAME`, `MONEYWEB_PASSWORD`)

### News Sources (RSS)

- Moneyweb
- BusinessTech
- News24 Business
- Daily Investor

## Notifications

Configure notifications in your `.env` file:

### Email

```env
NOTIFICATION_ENABLED=true
NOTIFICATION_EMAIL_SMTP_HOST=smtp.gmail.com
NOTIFICATION_EMAIL_SMTP_PORT=587
NOTIFICATION_EMAIL_FROM=your_email@gmail.com
NOTIFICATION_EMAIL_PASSWORD=your_app_password
```

### ntfy

```env
NOTIFICATION_NTFY_TOPIC=your_topic_name
```

### Discord

```env
NOTIFICATION_DISCORD_WEBHOOK_URL=your_webhook_url
```

### Telegram

```env
NOTIFICATION_TELEGRAM_BOT_TOKEN=your_bot_token
NOTIFICATION_TELEGRAM_CHAT_ID=your_chat_id
```

## Docker Deployment

### Build the Docker Image

```bash
docker build -t stock-analyzer .
```

### Run with Docker Compose

```bash
docker-compose up -d
```

### Manual Docker Run

```bash
docker run -d \
  -p 8501:8501 \
  -v $(pwd)/data:/app/data \
  --env-file .env \
  stock-analyzer
```

## Testing

Run tests with pytest:

```bash
pytest tests/
```

Run with coverage:

```bash
pytest tests/ --cov=app --cov-report=html
```

Tests use in-memory SQLite by default. The app runs on PostgreSQL, which
enforces things SQLite doesn't (column lengths), so run them there too by
pointing `TEST_DATABASE_URL` at a scratch database whose name contains `test`
(its tables are emptied between tests - never point it at real data):

```powershell
$env:TEST_DATABASE_URL = "postgresql://<user>:<pass>@localhost:5432/alphalens_test"
pytest tests/ --deselect tests/test_page_smoke.py
```

`tests/test_page_smoke.py` renders every dashboard page with all network access
refused, so a page that crashes when a provider is down fails the build.

A GitHub Actions workflow in `.github/workflows/ci.yml` runs the suite on
SQLite and on a PostgreSQL service container for every push and pull request.

## Troubleshooting

### Port 8501 is not available

The `AlphaLensDashboard` scheduled task already has the app running - it
starts at logon and restarts on crash, so the port is never free while
you're logged in. `.streamlit/config.toml` fixes the port at 8501, so
Streamlit fails instead of moving to another port. Pick one:

- **Just use it** - open http://localhost:8501; nothing needs starting.
- **Second copy alongside the task** (e.g. to watch console output):

  ```powershell
  .venv\Scripts\python.exe -m streamlit run main.py --server.port 8503
  ```

  then open http://localhost:8503. Both copies share the same database.
- **Swap the task for a terminal copy:** `schtasks /end /tn AlphaLensDashboard`,
  then `.venv\Scripts\python.exe scripts\run_dashboard.py`. Afterwards
  `schtasks /run /tn AlphaLensDashboard` (otherwise it returns at next logon).
- **Stop the automatic start** if you'd rather always launch it yourself:
  `scripts\uninstall_dashboard_task.ps1` - then `streamlit run main.py` works.

Use `.venv\Scripts\python.exe -m streamlit`, not a bare `streamlit`: the bare
command resolves to the global Python, not the venv's pinned packages.

Local port map: **8501** AlphaLens, **8502** fantasy-vibes (fixed in its
`.streamlit/config.toml` - start it from its repo root,
`streamlit run app/main.py`; from `app\` the config isn't read and it falls
back to 8501 on every interface). Use **8503+** for extra copies.

### Database Locked Error

If you encounter a SQLite database locked error, ensure only one instance of the application is running.

### API Rate Limits

The Twelve Data free tier has rate limits. If you hit the limit, the application will automatically fall back to Yahoo Finance.

### Missing Data

If stock data is missing, check:
1. Your API keys are correctly configured
2. The stock symbol is valid
3. The data source is operational

## Development

### Code Style

This project follows PEP 8 guidelines. Consider using:
- `black` for code formatting
- `flake8` for linting
- `mypy` for type checking

### Adding New Features

1. Create a feature branch
2. Implement your changes
3. Add tests
4. Update documentation
5. Submit a pull request

## Roadmap

### Version 1 (Current)
- ✅ Stock dashboard
- ✅ Watchlist
- ✅ Portfolio tracking with hold/buy/sell Action suggestions
- ✅ Historical prices
- ✅ Technical indicators
- ✅ Scheduled updates
- ✅ News aggregation
- ✅ Dividend tracking
- ✅ Notifications
- ✅ Commodity and Krugerrand tracking
- ✅ Penny-stock screener
- ✅ IPO tracker
- ✅ Crypto and net-worth tracking

### Version 2 (Planned)
- Push alert delivery to phone/email (Telegram, ntfy, email via Apprise - see [Notifications](#notifications)), with per-type filtering or a daily digest so time-critical alerts such as IPO offer windows reach you without opening the dashboard
- AI summaries
- Sentiment analysis
- Strategy backtesting
- Stock screening and risk analysis
- Portfolio optimization and Monte Carlo simulations

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests
5. Submit a pull request

## License

This project is open source and available under the MIT License.

## Disclaimer

This software is for educational purposes only. It does not constitute financial advice. Always do your own research before making investment decisions.

## Support

For issues, questions, or suggestions:
- Open an issue on GitHub
- Check the documentation
- Review the code comments

## Acknowledgments

- Twelve Data for providing market data API
- Yahoo Finance for backup data source
- Streamlit for the excellent UI framework
- The open-source community
