# JSE Stock Analysis Platform

An open-source, Python-based web application focused on providing investors with insights into companies listed on the Johannesburg Stock Exchange (JSE).

## Features

- **Real-time Market Data**: Track stock prices from multiple data sources
- **Watchlist Management**: Create and monitor your favorite stocks
- **Portfolio Tracking**: Track your investments and performance
- **News Aggregation**: Aggregate financial news from multiple South African sources
- **Technical Analysis**: Calculate and display technical indicators (RSI, MACD, Bollinger Bands, etc.)
- **Automated Data Collection**: Hourly updates via background scheduler
- **Notifications**: Get alerts for price changes, news, and technical signals
- **Interactive Charts**: Beautiful visualizations using Plotly

## Tech Stack

- **Frontend**: Streamlit, Plotly, AgGrid
- **Backend**: Python 3.13+
- **Database**: SQLite (development), PostgreSQL (production)
- **ORM**: SQLAlchemy
- **Scheduler**: APScheduler
- **Data Sources**: Twelve Data API, Yahoo Finance, RSS feeds
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
├── alembic/                # Database migrations
├── data/                   # Data directory (database, cache, logs)
├── docker/                 # Docker configuration
├── tests/                  # Test files
├── main.py                 # Main application entry point
├── requirements.txt        # Python dependencies
├── alembic.ini            # Alembic configuration
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
- `FMP_API_KEY`: Financial Modeling Prep API key for financial statements
- `EXCHANGE_RATE_API_KEY`: Exchange Rate API key for currency conversion
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

### Production Mode

For production deployment, consider using:

1. **Docker** (recommended)
2. **Gunicorn** with Streamlit
3. **Cloud platforms** (Streamlit Cloud, AWS, GCP, Azure)

See the Docker section below for containerized deployment.

## Usage

### Dashboard Navigation

The application has four main sections:

1. **Home**: Overview of market data, portfolio summary, and recent news
2. **Watchlist**: Manage your stock watchlist with price alerts
3. **Portfolio**: Track your holdings and performance
4. **Company View**: Detailed information about specific stocks

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

## Background Scheduler

The application includes a background scheduler that:

- Updates stock prices hourly
- Collects news from RSS feeds hourly
- Updates historical data daily
- Cleans up old articles weekly

The scheduler is automatically started when you run the application. To disable it, set `SCHEDULER_ENABLED=false` in your `.env` file.

## Database Migrations

### Create a New Migration

```bash
alembic revision --autogenerate -m "description of changes"
```

### Apply Migrations

```bash
alembic upgrade head
```

### Rollback Migration

```bash
alembic downgrade -1
```

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

## Troubleshooting

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
- ✅ Historical prices
- ✅ Hourly updates
- ✅ News aggregation

### Version 2 (Planned)
- Portfolio tracking
- Technical indicators
- Notifications
- Dividend tracking

### Version 3 (Future)
- AI summaries
- Local LLM integration
- Sentiment analysis
- Strategy backtesting

### Version 4 (Future)
- Stock screening
- Risk analysis
- Portfolio optimization
- Monte Carlo simulations

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
