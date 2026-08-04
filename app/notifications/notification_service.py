from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timezone
from app.models.notification import Notification
from app.models.stock import StockPrice
from app.models.watchlist import WatchlistItem
from app.config.settings import settings
from loguru import logger
import json
import apprise


class NotificationService:
    """Service for managing and sending notifications."""
    
    def __init__(self, db: Session):
        self.db = db
        self.enabled = settings.notification_enabled
        
        if self.enabled:
            self.appr = apprise.Apprise()
            self._setup_notification_channels()
    
    def _setup_notification_channels(self):
        """Setup notification channels based on configuration."""
        # Email
        if settings.notification_email_from and settings.notification_email_password:
            email_url = (
                f"mailto://{settings.notification_email_from}:"
                f"{settings.notification_email_password}@"
                f"{settings.notification_email_smtp_host}:"
                f"{settings.notification_email_smtp_port}"
            )
            self.appr.add(email_url)
            logger.info("Email notification channel configured")
        
        # ntfy
        if settings.notification_ntfy_topic:
            ntfy_url = f"ntfy://{settings.notification_ntfy_topic}"
            self.appr.add(ntfy_url)
            logger.info("ntfy notification channel configured")
        
        # Discord
        if settings.notification_discord_webhook_url:
            discord_url = f"discord://{settings.notification_discord_webhook_url}"
            self.appr.add(discord_url)
            logger.info("Discord notification channel configured")
        
        # Telegram
        if settings.notification_telegram_bot_token and settings.notification_telegram_chat_id:
            telegram_url = (
                f"tgram://{settings.notification_telegram_bot_token}/"
                f"{settings.notification_telegram_chat_id}"
            )
            self.appr.add(telegram_url)
            logger.info("Telegram notification channel configured")
    
    def create_notification(
        self,
        notification_type: str,
        title: str,
        message: str,
        stock_symbol: Optional[str] = None,
        metadata: Optional[dict] = None
    ) -> Notification:
        """Create a notification in the database."""
        notification = Notification(
            type=notification_type,
            title=title,
            message=message,
            stock_symbol=stock_symbol,
            meta_data=json.dumps(metadata) if metadata else None
        )
        
        self.db.add(notification)
        self.db.commit()
        
        return notification
    
    def send_notification(self, notification: Notification, channel: Optional[str] = None) -> bool:
        """Send a notification through configured channels."""
        if not self.enabled:
            logger.info("Notifications are disabled")
            return False
        
        try:
            # Prepare notification body
            body = notification.message
            if notification.stock_symbol:
                body = f"[{notification.stock_symbol}] {body}"
            
            # Send notification
            if channel:
                # Send to specific channel
                success = self.appr.notify(
                    title=notification.title,
                    body=body,
                    tag=channel
                )
            else:
                # Send to all configured channels
                success = self.appr.notify(
                    title=notification.title,
                    body=body
                )
            
            if success:
                notification.is_sent = True
                notification.sent_at = datetime.now(timezone.utc)
                notification.delivery_channel = channel or "all"
                self.db.commit()
                logger.info(f"Notification sent: {notification.title}")
                return True
            else:
                logger.error(f"Failed to send notification: {notification.title}")
                return False
        
        except Exception as e:
            logger.error(f"Error sending notification: {e}")
            return False
    
    def check_price_alerts(self):
        """Check watchlist items for price alerts."""
        if not self.enabled:
            return
        
        watchlist_items = self.db.query(WatchlistItem).filter(
            WatchlistItem.is_active == True
        ).all()
        
        for item in watchlist_items:
            latest_price = self.db.query(StockPrice).filter(
                StockPrice.symbol == item.symbol
            ).order_by(StockPrice.timestamp.desc()).first()
            
            if not latest_price:
                continue
            
            current_price = latest_price.close_price
            
            # Check target price
            if item.target_price and current_price >= item.target_price:
                self.create_notification(
                    notification_type="price",
                    title=f"Target Price Reached: {item.symbol}",
                    message=f"{item.symbol} has reached your target price of R {item.target_price:.2f}. Current price: R {current_price:.2f}",
                    stock_symbol=item.symbol,
                    metadata={"target_price": item.target_price, "current_price": current_price}
                )
            
            # Check stop loss
            if item.stop_loss and current_price <= item.stop_loss:
                self.create_notification(
                    notification_type="price",
                    title=f"Stop Loss Triggered: {item.symbol}",
                    message=f"{item.symbol} has hit your stop loss at R {item.stop_loss:.2f}. Current price: R {current_price:.2f}",
                    stock_symbol=item.symbol,
                    metadata={"stop_loss": item.stop_loss, "current_price": current_price}
                )
            
            # Check price change threshold
            if item.notify_price_increase or item.notify_price_decrease:
                prev_price = self.db.query(StockPrice).filter(
                    StockPrice.symbol == item.symbol,
                    StockPrice.timestamp < latest_price.timestamp
                ).order_by(StockPrice.timestamp.desc()).first()
                
                if prev_price:
                    change_pct = ((current_price - prev_price.close_price) / prev_price.close_price) * 100
                    
                    if item.notify_price_increase and change_pct >= item.price_change_threshold:
                        self.create_notification(
                            notification_type="price",
                            title=f"Price Increase Alert: {item.symbol}",
                            message=f"{item.symbol} has increased by {change_pct:.2f}%. Previous: R {prev_price.close_price:.2f}, Current: R {current_price:.2f}",
                            stock_symbol=item.symbol,
                            metadata={"change_percentage": change_pct}
                        )
                    
                    if item.notify_price_decrease and change_pct <= -item.price_change_threshold:
                        self.create_notification(
                            notification_type="price",
                            title=f"Price Decrease Alert: {item.symbol}",
                            message=f"{item.symbol} has decreased by {abs(change_pct):.2f}%. Previous: R {prev_price.close_price:.2f}, Current: R {current_price:.2f}",
                            stock_symbol=item.symbol,
                            metadata={"change_percentage": change_pct}
                        )
    
    def check_technical_alerts(self):
        """Check for technical indicator alerts."""
        if not self.enabled:
            return
        
        watchlist_items = self.db.query(WatchlistItem).filter(
            WatchlistItem.is_active == True
        ).all()
        
        for item in watchlist_items:
            latest_price = self.db.query(StockPrice).filter(
                StockPrice.symbol == item.symbol
            ).order_by(StockPrice.timestamp.desc()).first()
            
            if not latest_price:
                continue
            
            # RSI overbought/oversold
            if latest_price.rsi:
                if latest_price.rsi > 70:
                    self.create_notification(
                        notification_type="technical",
                        title=f"RSI Overbought: {item.symbol}",
                        message=f"{item.symbol} RSI is {latest_price.rsi:.2f}, indicating overbought conditions.",
                        stock_symbol=item.symbol,
                        metadata={"indicator": "RSI", "value": latest_price.rsi}
                    )
                elif latest_price.rsi < 30:
                    self.create_notification(
                        notification_type="technical",
                        title=f"RSI Oversold: {item.symbol}",
                        message=f"{item.symbol} RSI is {latest_price.rsi:.2f}, indicating oversold conditions.",
                        stock_symbol=item.symbol,
                        metadata={"indicator": "RSI", "value": latest_price.rsi}
                    )
            
            # MACD crossover
            if latest_price.macd and latest_price.macd_signal:
                prev_price = self.db.query(StockPrice).filter(
                    StockPrice.symbol == item.symbol,
                    StockPrice.timestamp < latest_price.timestamp
                ).order_by(StockPrice.timestamp.desc()).first()
                
                if prev_price and prev_price.macd and prev_price.macd_signal:
                    # Bullish crossover
                    if prev_price.macd <= prev_price.macd_signal and latest_price.macd > latest_price.macd_signal:
                        self.create_notification(
                            notification_type="technical",
                            title=f"MACD Bullish Crossover: {item.symbol}",
                            message=f"{item.symbol} MACD has crossed above the signal line, potential bullish signal.",
                            stock_symbol=item.symbol,
                            metadata={"indicator": "MACD", "signal": "bullish_crossover"}
                        )
                    # Bearish crossover
                    elif prev_price.macd >= prev_price.macd_signal and latest_price.macd < latest_price.macd_signal:
                        self.create_notification(
                            notification_type="technical",
                            title=f"MACD Bearish Crossover: {item.symbol}",
                            message=f"{item.symbol} MACD has crossed below the signal line, potential bearish signal.",
                            stock_symbol=item.symbol,
                            metadata={"indicator": "MACD", "signal": "bearish_crossover"}
                        )
    
    def send_pending_notifications(self):
        """Send all pending notifications."""
        if not self.enabled:
            return
        
        pending = self.db.query(Notification).filter(
            Notification.is_sent == False
        ).all()
        
        for notification in pending:
            self.send_notification(notification)
    
    def get_unread_notifications(self, limit: int = 50) -> List[Notification]:
        """Get unread notifications."""
        return self.db.query(Notification).filter(
            Notification.is_read == False
        ).order_by(Notification.created_at.desc()).limit(limit).all()
    
    def mark_as_read(self, notification_id: int):
        """Mark a notification as read."""
        notification = self.db.query(Notification).filter(
            Notification.id == notification_id
        ).first()
        
        if notification:
            notification.is_read = True
            notification.read_at = datetime.now(timezone.utc)
            self.db.commit()
    
    def mark_all_as_read(self):
        """Mark all notifications as read."""
        self.db.query(Notification).filter(
            Notification.is_read == False
        ).update({
            "is_read": True,
            "read_at": datetime.now(timezone.utc)
        })
        self.db.commit()
