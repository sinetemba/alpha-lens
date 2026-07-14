from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session
from app.models.base import SessionLocal
from app.services.data_service import DataService
from app.scrapers.news import NewsScraper
from app.config.settings import settings
from loguru import logger


class SchedulerService:
    """Service for managing scheduled background jobs."""
    
    def __init__(self):
        self.scheduler = BackgroundScheduler(timezone=settings.scheduler_timezone)
        self.enabled = settings.scheduler_enabled
    
    def start(self):
        """Start the scheduler with all configured jobs."""
        if not self.enabled:
            logger.info("Scheduler is disabled in configuration")
            return
        
        logger.info("Starting scheduler")
        
        # Hourly stock price updates
        self.scheduler.add_job(
            self._hourly_price_update,
            trigger=CronTrigger(
                hour=settings.hourly_update_hour,
                minute=settings.hourly_update_minute
            ),
            id="hourly_price_update",
            name="Hourly Stock Price Update",
            replace_existing=True
        )
        
        # Hourly news collection
        self.scheduler.add_job(
            self._hourly_news_collection,
            trigger=CronTrigger(
                hour=settings.hourly_update_hour,
                minute=settings.hourly_update_minute
            ),
            id="hourly_news_collection",
            name="Hourly News Collection",
            replace_existing=True
        )
        
        # Daily historical data update (at midnight)
        self.scheduler.add_job(
            self._daily_historical_update,
            trigger=CronTrigger(hour=0, minute=0),
            id="daily_historical_update",
            name="Daily Historical Data Update",
            replace_existing=True
        )
        
        # Clean old news (weekly)
        self.scheduler.add_job(
            self._weekly_cleanup,
            trigger=CronTrigger(day_of_week="sun", hour=2, minute=0),
            id="weekly_cleanup",
            name="Weekly Cleanup",
            replace_existing=True
        )
        
        self.scheduler.start()
        logger.info("Scheduler started successfully")
    
    def stop(self):
        """Stop the scheduler."""
        logger.info("Stopping scheduler")
        self.scheduler.shutdown()
        logger.info("Scheduler stopped")
    
    def _hourly_price_update(self):
        """Job: Update stock prices hourly."""
        logger.info("Running hourly price update job")
        
        db = SessionLocal()
        try:
            data_service = DataService(db)
            updated = data_service.update_stock_prices()
            logger.info(f"Hourly price update completed: {updated} symbols updated")
        except Exception as e:
            logger.error(f"Error in hourly price update: {e}")
        finally:
            db.close()
    
    def _hourly_news_collection(self):
        """Job: Collect news hourly."""
        logger.info("Running hourly news collection job")
        
        db = SessionLocal()
        try:
            news_scraper = NewsScraper(db)
            articles = news_scraper.scrape_all_sources()
            logger.info(f"Hourly news collection completed: {len(articles)} articles collected")
        except Exception as e:
            logger.error(f"Error in hourly news collection: {e}")
        finally:
            db.close()
    
    def _daily_historical_update(self):
        """Job: Update historical data daily."""
        logger.info("Running daily historical update job")
        
        db = SessionLocal()
        try:
            data_service = DataService(db)
            
            # Get watchlist symbols
            from app.models.watchlist import WatchlistItem
            watchlist_items = db.query(WatchlistItem).filter(
                WatchlistItem.is_active == True
            ).all()
            
            symbols = list(set([item.symbol for item in watchlist_items]))
            
            if not symbols:
                # Use default watchlist
                symbols = settings.default_watchlist_symbols
            
            for symbol in symbols:
                data_service.update_historical_data(symbol, days=365)
            
            logger.info(f"Daily historical update completed for {len(symbols)} symbols")
        except Exception as e:
            logger.error(f"Error in daily historical update: {e}")
        finally:
            db.close()
    
    def _weekly_cleanup(self):
        """Job: Clean up old data weekly."""
        logger.info("Running weekly cleanup job")
        
        db = SessionLocal()
        try:
            news_scraper = NewsScraper(db)
            deleted = news_scraper.clean_old_articles(days=90)
            logger.info(f"Weekly cleanup completed: {deleted} old articles deleted")
        except Exception as e:
            logger.error(f"Error in weekly cleanup: {e}")
        finally:
            db.close()
    
    def get_jobs(self):
        """Get list of scheduled jobs."""
        return self.scheduler.get_jobs()
    
    def run_job_now(self, job_id: str):
        """Run a specific job immediately."""
        job = self.scheduler.get_job(job_id)
        if job:
            job.func()
            logger.info(f"Job {job_id} executed manually")
        else:
            logger.warning(f"Job {job_id} not found")
