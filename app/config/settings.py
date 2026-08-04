from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from functools import lru_cache
from typing import List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    # Database Configuration
    database_url: str = Field(default="sqlite:///./data/stock_analyzer.db", alias="DATABASE_URL")

    # Twelve Data API
    twelve_data_api_key: str = Field(default="", alias="TWELVE_DATA_API_KEY")
    twelve_data_base_url: str = Field(default="https://api.twelvedata.com", alias="TWELVE_DATA_BASE_URL")

    # Yahoo Finance
    yfinance_enabled: bool = Field(default=True, alias="YFINANCE_ENABLED")

    # EasyEquities (unofficial client - logs in with account credentials)
    easyequities_username: str = Field(default="", alias="EASYEQUITIES_USERNAME")
    easyequities_password: str = Field(default="", alias="EASYEQUITIES_PASSWORD")

    # Financial Modeling Prep API
    fmp_api_key: str = Field(default="", alias="FMP_API_KEY")
    fmp_base_url: str = Field(default="https://financialmodelingprep.com/api/v3", alias="FMP_BASE_URL")

    # GoldAPI.io
    gold_api_key: str = Field(default="", alias="GOLD_API_KEY")
    gold_api_base_url: str = Field(default="https://www.goldapi.io", alias="GOLD_API_BASE_URL")

    # CommodityPriceAPI
    commodity_price_api_key: str = Field(default="", alias="COMMODITY_PRICE_API_KEY")
    commodity_price_api_base_url: str = Field(default="https://api.commoditypriceapi.com/v2", alias="COMMODITY_PRICE_API_BASE_URL")
    metals_dev_api_key: str = Field(default="", alias="METALS_DEV_API_KEY")
    metals_dev_base_url: str = Field(default="https://api.metals.dev/v1/latest", alias="METALS_DEV_BASE_URL")
    metals_dev_currency: str = Field(default="ZAR", alias="METALS_DEV_CURRENCY")
    metals_dev_unit: str = Field(default="g", alias="METALS_DEV_UNIT")
    alpha_vantage_api_key: str = Field(default="", alias="ALPHA_VANTAGE_API_KEY")
    alpha_vantage_base_url: str = Field(default="https://www.alphavantage.co/query", alias="ALPHA_VANTAGE_BASE_URL")

    # Exchange Rate API
    exchange_rate_api_key: str = Field(default="", alias="EXCHANGE_RATE_API_KEY")
    exchange_rate_base_url: str = Field(default="https://v6.exchangerate-api.com/v6", alias="EXCHANGE_RATE_BASE_URL")

    # Scheduler Configuration
    scheduler_enabled: bool = Field(default=True, alias="SCHEDULER_ENABLED")
    scheduler_timezone: str = Field(default="Africa/Johannesburg", alias="SCHEDULER_TIMEZONE")
    hourly_update_hour: str = Field(default="*", alias="HOURLY_UPDATE_HOUR")
    hourly_update_minute: str = Field(default="0", alias="HOURLY_UPDATE_MINUTE")

    # RSS Feed URLs
    rss_moneyweb: str = Field(default="https://www.moneyweb.co.za/feed/", alias="RSS_MONEYWEB")
    rss_businesstech: str = Field(default="https://businesstech.co.za/news/feed/", alias="RSS_BUSINESSTECH")
    rss_news24_business: str = Field(default="https://www.news24.com/Services/Site/Feeds/24/Article/TopStories?sectionId=3", alias="RSS_NEWS24_BUSINESS")
    rss_daily_investor: str = Field(default="https://www.dailyinvestor.com/feed/", alias="RSS_DAILY_INVESTOR")

    # Caching Configuration
    cache_type: str = Field(default="diskcache", alias="CACHE_TYPE")
    cache_dir: str = Field(default="./data/cache", alias="CACHE_DIR")
    cache_ttl_seconds: int = Field(default=3600, alias="CACHE_TTL_SECONDS")

    # Notification Configuration
    notification_enabled: bool = Field(default=False, alias="NOTIFICATION_ENABLED")
    notification_email_smtp_host: str = Field(default="smtp.gmail.com", alias="NOTIFICATION_EMAIL_SMTP_HOST")
    notification_email_smtp_port: int = Field(default=587, alias="NOTIFICATION_EMAIL_SMTP_PORT")
    notification_email_from: str = Field(default="", alias="NOTIFICATION_EMAIL_FROM")
    notification_email_password: str = Field(default="", alias="NOTIFICATION_EMAIL_PASSWORD")
    notification_ntfy_topic: str = Field(default="", alias="NOTIFICATION_NTFY_TOPIC")
    notification_discord_webhook_url: str = Field(default="", alias="NOTIFICATION_DISCORD_WEBHOOK_URL")
    notification_telegram_bot_token: str = Field(default="", alias="NOTIFICATION_TELEGRAM_BOT_TOKEN")
    notification_telegram_chat_id: str = Field(default="", alias="NOTIFICATION_TELEGRAM_CHAT_ID")

    # Streamlit Configuration
    streamlit_server_port: int = Field(default=8501, alias="STREAMLIT_SERVER_PORT")
    streamlit_server_address: str = Field(default="localhost", alias="STREAMLIT_SERVER_ADDRESS")

    # Logging Configuration
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_file: str = Field(default="./data/logs/app.log", alias="LOG_FILE")

    # JSE Configuration
    jse_index_symbol: str = Field(default="J203", alias="JSE_INDEX_SYMBOL")
    default_watchlist: str = Field(default="NPN,CFR,AGL,SOL,SBK", alias="DEFAULT_WATCHLIST")

    @property
    def rss_feeds(self) -> List[str]:
        """Return list of RSS feed URLs."""
        return [
            self.rss_moneyweb,
            self.rss_businesstech,
            self.rss_news24_business,
            self.rss_daily_investor,
        ]

    @property
    def default_watchlist_symbols(self) -> List[str]:
        """Return default watchlist as list of symbols."""
        return [s.strip() for s in self.default_watchlist.split(",") if s.strip()]

    @property
    def database_path(self) -> str:
        """Extract database path from database URL."""
        if self.database_url.startswith("sqlite:///"):
            return self.database_url.replace("sqlite:///", "")
        return self.database_url


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


settings = get_settings()
