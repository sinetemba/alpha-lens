from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from app.models.notification import Notification
from app.models.news import NewsArticle
from app.models.stock import Stock
from app.models.watchlist import WatchlistItem
# app.services.price_queries is Streamlit-free so this service also runs inside
# the headless updater (importing app.dashboard.home would drag Streamlit and a
# ThreadPoolExecutor into that process).
from app.services.price_queries import get_latest_and_previous_prices
from app.utils.formatting import format_price
from app.config.settings import settings
from loguru import logger
import json
from urllib.parse import quote

import apprise


# Failed sends are retried on each run until this cap, then left in the table
# (still visible to a future notifications page) but no longer re-sent.
MAX_SEND_ATTEMPTS = 5

# Identical alerts (same type/title/symbol) within this window reuse the
# existing row - a persistent condition like a price parked above target must
# not create a new notification on every update run.
ALERT_DEDUP_WINDOW = timedelta(hours=20)


def _utcnow_naive() -> datetime:
    """Current UTC time as a naive datetime, matching the naive DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class NotificationService:
    """Service for managing and sending notifications."""

    def __init__(self, db: Session):
        self.db = db
        self.enabled = settings.notification_enabled
        # (type, title, symbol) -> row for the dedup window, loaded lazily so
        # a burst of alert checks costs one query instead of one per alert.
        self._recent_by_key: Optional[dict] = None
        # title -> latest created_at for _alerted_since: one grouped query
        # lazily instead of a SELECT per watchlist item per alert check
        # (Notification.title is unindexed, so each lookup was a full scan).
        self._alert_latest_by_title: Optional[dict] = None

        if self.enabled:
            self.appr = apprise.Apprise()
            self._setup_notification_channels()

    def _setup_notification_channels(self):
        """Setup notification channels based on configuration."""
        # Email
        if settings.notification_email_from and settings.notification_email_password:
            # The password must be URL-encoded - an SMTP password containing
            # '@', ':' or '%' would corrupt the authority parse and fail sends.
            email_url = (
                f"mailto://{settings.notification_email_from}:"
                f"{quote(settings.notification_email_password, safe='')}@"
                f"{settings.notification_email_smtp_host}:"
                f"{settings.notification_email_smtp_port}"
            )
            self.appr.add(email_url, tag="email")
            logger.info("Email notification channel configured")

        # ntfy
        if settings.notification_ntfy_topic:
            ntfy_url = f"ntfy://{settings.notification_ntfy_topic}"
            self.appr.add(ntfy_url, tag="ntfy")
            logger.info("ntfy notification channel configured")

        # Discord
        if settings.notification_discord_webhook_url:
            discord_url = f"discord://{settings.notification_discord_webhook_url}"
            self.appr.add(discord_url, tag="discord")
            logger.info("Discord notification channel configured")

        # Telegram
        if settings.notification_telegram_bot_token and settings.notification_telegram_chat_id:
            telegram_url = (
                f"tgram://{settings.notification_telegram_bot_token}/"
                f"{settings.notification_telegram_chat_id}"
            )
            self.appr.add(telegram_url, tag="telegram")
            logger.info("Telegram notification channel configured")

    @staticmethod
    def channel_status() -> dict[str, bool]:
        """Which delivery channels have their required settings filled in."""
        return {
            "email": bool(settings.notification_email_from and settings.notification_email_password),
            "ntfy": bool(settings.notification_ntfy_topic),
            "discord": bool(settings.notification_discord_webhook_url),
            "telegram": bool(
                settings.notification_telegram_bot_token and settings.notification_telegram_chat_id
            ),
        }

    def send_test(self, channel: Optional[str] = None) -> tuple[bool, str]:
        """Send a test message without creating a Notification row."""
        if not self.enabled:
            return False, "Delivery is disabled (NOTIFICATION_ENABLED=false)."
        if not any(self.channel_status().values()):
            return False, "No delivery channel is configured in .env."
        try:
            ok = self.appr.notify(
                title="AlphaLens test notification",
                body="If you can read this, alert delivery works.",
                tag=channel or apprise.common.MATCH_ALL_TAG,
            )
        except Exception as e:
            logger.error(f"Test notification failed: {e}")
            return False, "Sending failed - see logs for details."
        return (True, "Test notification sent.") if ok else (
            False, "The channel rejected the message - check its settings in .env."
        )

    def create_notification(
        self,
        notification_type: str,
        title: str,
        message: str,
        stock_symbol: Optional[str] = None,
        metadata: Optional[dict] = None
    ) -> Notification:
        """Create a notification in the database.

        Identical alerts (same type, title and symbol) raised within
        ALERT_DEDUP_WINDOW return the existing row instead of inserting a
        duplicate on every update run.
        """
        if self._recent_by_key is None:
            cutoff = _utcnow_naive() - ALERT_DEDUP_WINDOW
            self._recent_by_key = {
                (row.type, row.title, row.stock_symbol): row
                for row in self.db.query(Notification).filter(
                    Notification.created_at >= cutoff
                ).all()
            }
        key = (notification_type, title, stock_symbol)
        existing = self._recent_by_key.get(key)
        if existing:
            return existing

        notification = Notification(
            type=notification_type,
            title=title,
            message=message,
            stock_symbol=stock_symbol,
            meta_data=json.dumps(metadata) if metadata else None,
            # Delivery disabled = in-app channel only: mark sent so the row
            # never queues for external delivery - otherwise the day delivery
            # is first enabled, weeks of stale alerts flush at once.
            is_sent=not self.enabled,
            delivery_channel=None if self.enabled else "in-app",
        )

        self.db.add(notification)
        self.db.commit()
        self._recent_by_key[key] = notification
        if self._alert_latest_by_title is not None:
            # A freshly-raised alert must count for later same-title checks
            # in this run, or the crossing edge-fires twice.
            self._alert_latest_by_title[title] = notification.created_at

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
                notification.sent_at = _utcnow_naive()
                notification.delivery_channel = channel or "all"
                self.db.commit()
                logger.info(f"Notification sent: {notification.title}")
                return True

            logger.error(f"Failed to send notification: {notification.title}")

        except Exception as e:
            # Don't log `str(e)` - Apprise exceptions can echo the asset URL,
            # which may contain webhook/bot tokens or the mailto password.
            logger.error(f"Error sending notification: {e.__class__.__name__}")

        notification.attempts = (notification.attempts or 0) + 1
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
        return False

    def _alertable_watchlist_items(self) -> List:
        """Active watchlist items whose stock is still active.

        Deactivated stocks (delisted/bad tickers) are not tracked by the
        updater, so their stored prices are frozen - alerting against them
        is noise (e.g. a delisted stock permanently above its target).
        """
        return self.db.query(WatchlistItem).join(
            Stock, WatchlistItem.stock_id == Stock.id
        ).filter(
            WatchlistItem.is_active == True,
            Stock.is_active == True,
        ).all()

    def check_price_alerts(self):
        """Check watchlist items for price alerts.

        Runs even when external delivery is disabled: the Notifications page
        is an in-app alert channel of its own, so rows are created
        unconditionally and only sending is gated on self.enabled.
        """
        watchlist_items = self._alertable_watchlist_items()

        if not watchlist_items:
            return

        symbols = [item.symbol for item in watchlist_items]
        prices = get_latest_and_previous_prices(self.db, symbols)

        for item in watchlist_items:
            latest, prev = prices.get(item.symbol, (None, None))
            if not latest:
                continue

            current_price = latest.close_price
            prev_close = prev.close_price if prev and prev.close_price else None

            # Target / stop are edge-triggered: alert when the price CROSSES
            # the level, not every run while it stays beyond it (SOL, NPK and
            # DRD each alerted 4 days running). A level set while already
            # crossed alerts once (no prior alert since the item was edited).
            if item.target_price and current_price >= item.target_price and (
                (prev_close is not None and prev_close < item.target_price)
                or not self._alerted_since(f"Target Price Reached: {item.symbol}", item.updated_at)
            ):
                self.create_notification(
                    notification_type="price",
                    title=f"Target Price Reached: {item.symbol}",
                    message=f"{item.symbol} has reached your target price of {format_price(item.target_price)}. Current price: {format_price(current_price)}",
                    stock_symbol=item.symbol,
                    metadata={"target_price": item.target_price, "current_price": current_price}
                )

            # Check stop loss (edge-triggered, as above)
            if item.stop_loss and current_price <= item.stop_loss and (
                (prev_close is not None and prev_close > item.stop_loss)
                or not self._alerted_since(f"Stop Loss Triggered: {item.symbol}", item.updated_at)
            ):
                self.create_notification(
                    notification_type="price",
                    title=f"Stop Loss Triggered: {item.symbol}",
                    message=f"{item.symbol} has hit your stop loss at {format_price(item.stop_loss)}. Current price: {format_price(current_price)}",
                    stock_symbol=item.symbol,
                    metadata={"stop_loss": item.stop_loss, "current_price": current_price}
                )

            # Check price change threshold
            if (item.notify_price_increase or item.notify_price_decrease) and prev and prev.close_price:
                change_pct = ((current_price - prev.close_price) / prev.close_price) * 100

                if item.notify_price_increase and change_pct >= item.price_change_threshold:
                    self.create_notification(
                        notification_type="price",
                        title=f"Price Increase Alert: {item.symbol}",
                        message=f"{item.symbol} has increased by {change_pct:.2f}%. Previous: {format_price(prev.close_price)}, Current: {format_price(current_price)}",
                        stock_symbol=item.symbol,
                        metadata={"change_percentage": change_pct}
                    )

                if item.notify_price_decrease and change_pct <= -item.price_change_threshold:
                    self.create_notification(
                        notification_type="price",
                        title=f"Price Decrease Alert: {item.symbol}",
                        message=f"{item.symbol} has decreased by {abs(change_pct):.2f}%. Previous: {format_price(prev.close_price)}, Current: {format_price(current_price)}",
                        stock_symbol=item.symbol,
                        metadata={"change_percentage": change_pct}
                    )

    def _alerted_since(self, title: str, since: Optional[datetime]) -> bool:
        """Whether an alert with this title exists since `since` (any time if None)."""
        if self._alert_latest_by_title is None:
            self._alert_latest_by_title = dict(
                self.db.query(
                    Notification.title, func.max(Notification.created_at)
                ).group_by(Notification.title).all()
            )
        latest = self._alert_latest_by_title.get(title)
        return latest is not None and (since is None or latest >= since)

    def check_technical_alerts(self):
        """Check for technical indicator alerts (see check_price_alerts re: enabled)."""
        watchlist_items = self._alertable_watchlist_items()

        if not watchlist_items:
            return

        symbols = [item.symbol for item in watchlist_items]
        prices = get_latest_and_previous_prices(self.db, symbols)

        for item in watchlist_items:
            latest, prev = prices.get(item.symbol, (None, None))
            if not latest:
                continue

            # RSI overbought/oversold - on ENTERING the zone, not every run
            # spent inside it (SKA "RSI Oversold" repeated daily).
            prev_rsi = prev.rsi if prev is not None else None
            if latest.rsi is not None:
                if latest.rsi > 70 and (prev_rsi is None or prev_rsi <= 70):
                    self.create_notification(
                        notification_type="technical",
                        title=f"RSI Overbought: {item.symbol}",
                        message=f"{item.symbol} RSI is {latest.rsi:.2f}, indicating overbought conditions.",
                        stock_symbol=item.symbol,
                        metadata={"indicator": "RSI", "value": latest.rsi}
                    )
                elif latest.rsi < 30 and (prev_rsi is None or prev_rsi >= 30):
                    self.create_notification(
                        notification_type="technical",
                        title=f"RSI Oversold: {item.symbol}",
                        message=f"{item.symbol} RSI is {latest.rsi:.2f}, indicating oversold conditions.",
                        stock_symbol=item.symbol,
                        metadata={"indicator": "RSI", "value": latest.rsi}
                    )

            # MACD crossover (0.0 is a legitimate value - check for None, not truthiness)
            if (
                latest.macd is not None and latest.macd_signal is not None
                and prev and prev.macd is not None and prev.macd_signal is not None
            ):
                # Bullish crossover
                if prev.macd <= prev.macd_signal and latest.macd > latest.macd_signal:
                    self.create_notification(
                        notification_type="technical",
                        title=f"MACD Bullish Crossover: {item.symbol}",
                        message=f"{item.symbol} MACD has crossed above the signal line, potential bullish signal.",
                        stock_symbol=item.symbol,
                        metadata={"indicator": "MACD", "signal": "bullish_crossover"}
                    )
                # Bearish crossover
                elif prev.macd >= prev.macd_signal and latest.macd < latest.macd_signal:
                    self.create_notification(
                        notification_type="technical",
                        title=f"MACD Bearish Crossover: {item.symbol}",
                        message=f"{item.symbol} MACD has crossed below the signal line, potential bearish signal.",
                        stock_symbol=item.symbol,
                        metadata={"indicator": "MACD", "signal": "bearish_crossover"}
                    )

    def check_news_alerts(self):
        """Raise type="news" alerts for fresh articles about watchlisted symbols.

        Consumes WatchlistItem.notify_news - the flag existed with no reader.
        The scan window equals ALERT_DEDUP_WINDOW so each stored article
        alerts exactly once: within the window create_notification dedups
        repeats, and outside it the article isn't re-scanned.
        """
        items = [i for i in self._alertable_watchlist_items() if i.notify_news]
        if not items:
            return
        symbols = {i.symbol for i in items}
        cutoff = _utcnow_naive() - ALERT_DEDUP_WINDOW
        articles = self.db.query(NewsArticle).filter(
            NewsArticle.stock_symbol.in_(symbols),
            NewsArticle.created_at >= cutoff,
        ).order_by(NewsArticle.created_at.desc()).limit(100).all()

        for article in articles:
            headline = (article.title or "").strip()
            if not headline:
                continue
            self.create_notification(
                notification_type="news",
                title=headline[:490],
                message=(
                    f"{article.source}: {(article.summary or '').strip() or article.url}"
                )[:2000],
                stock_symbol=article.stock_symbol,
                metadata={"url": article.url, "source": article.source},
            )

    def send_pending_notifications(self):
        """Send pending notifications, newest first, within the last day only.

        Rows older than a day are skipped: an alert that stale is noise, and
        a permanently failing row must not head-of-line block fresh ones.
        """
        if not self.enabled:
            return

        pending = self.db.query(Notification).filter(
            Notification.is_sent == False,
            or_(Notification.attempts.is_(None), Notification.attempts < MAX_SEND_ATTEMPTS),
            Notification.created_at >= _utcnow_naive() - timedelta(hours=24),
        ).order_by(Notification.created_at.desc()).limit(200).all()

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
            notification.read_at = _utcnow_naive()
            self.db.commit()

    def mark_all_as_read(self):
        """Mark all notifications as read."""
        self.db.query(Notification).filter(
            Notification.is_read == False
        ).update({
            "is_read": True,
            "read_at": _utcnow_naive()
        })
        self.db.commit()
