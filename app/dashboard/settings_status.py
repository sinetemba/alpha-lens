"""Settings & status page: what is configured, and whether the background jobs are healthy.

Read-only by design - every setting lives in `.env`, which this page never
writes (secrets typed into a browser form would end up in a plaintext file
anyway). It shows which values are set, never the values themselves.
"""

from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config.settings import settings
from app.dashboard.utils import _to_display_tz, format_twelve_data_quota
from app.models.updater_run import UpdaterRun
from app.notifications.notification_service import NotificationService
from app.services import backup

# Heartbeat job names written by the updater -> what the user sees.
_JOB_LABELS = {
    "price_news_update": "Prices + news (3x daily)",
    "daily_historical": "History + dividends (daily)",
    "weekly_cleanup": "Old news cleanup (weekly)",
    "database_backup": "Database backup (weekly)",
}


def show_settings(db: Session):
    st.header("⚙️ Settings & Status")
    st.caption(
        "Settings are read from the `.env` file in the app folder. Edit it, then restart the "
        "dashboard and the updater (`schtasks /end /tn AlphaLensUpdater` then "
        "`schtasks /run /tn AlphaLensUpdater`) for changes to apply."
    )

    _render_notifications(db)
    st.markdown("---")
    _render_updater_status(db)
    st.markdown("---")
    _render_backups()
    st.markdown("---")
    _render_data_sources()


def _yes_no(value: bool) -> str:
    return "✅ Set" if value else "— Not set"


def _render_notifications(db: Session):
    st.subheader("🔔 Alert delivery")
    channels = NotificationService.channel_status()
    configured = [name for name, ok in channels.items() if ok]

    if not settings.notification_enabled:
        st.info(
            "External delivery is **off** (`NOTIFICATION_ENABLED=false`). Alerts are still "
            "recorded on the Notifications page."
        )
    elif not configured:
        st.warning(
            "Delivery is **on** but no channel is configured, so every alert send fails "
            "(you'll see 'Failed to send notification' in the updater log). Fill in one "
            "channel below in `.env`, or set `NOTIFICATION_ENABLED=false`. Alerts are still "
            "recorded on the Notifications page either way."
        )

    st.dataframe(
        pd.DataFrame([
            {"Channel": "Email", "Status": _yes_no(channels["email"]),
             "Needs": "NOTIFICATION_EMAIL_FROM, NOTIFICATION_EMAIL_PASSWORD (+ SMTP host/port)"},
            {"Channel": "ntfy", "Status": _yes_no(channels["ntfy"]), "Needs": "NOTIFICATION_NTFY_TOPIC"},
            {"Channel": "Discord", "Status": _yes_no(channels["discord"]),
             "Needs": "NOTIFICATION_DISCORD_WEBHOOK_URL"},
            {"Channel": "Telegram", "Status": _yes_no(channels["telegram"]),
             "Needs": "NOTIFICATION_TELEGRAM_BOT_TOKEN, NOTIFICATION_TELEGRAM_CHAT_ID"},
        ]),
        hide_index=True,
        use_container_width=True,
    )
    st.caption(
        "Per-stock alert rules (target, stop loss, daily-move threshold) are set on the "
        "Watchlist page when adding or editing a stock."
    )

    flash = st.session_state.pop("settings_test_flash", None)
    if flash:
        level, message = flash
        getattr(st, level)(message)

    can_test = settings.notification_enabled and bool(configured)
    col1, col2 = st.columns([2, 1])
    with col1:
        target = st.selectbox(
            "Send a test to", ["All configured channels"] + configured,
            disabled=not can_test, key="settings_test_channel",
        )
    with col2:
        st.write("")
        if st.button("Send test notification", disabled=not can_test, key="settings_test_send"):
            with st.spinner("Sending..."):
                ok, message = NotificationService(db).send_test(
                    None if target == "All configured channels" else target
                )
            st.session_state["settings_test_flash"] = ("success" if ok else "error", message)
            st.rerun()


def _render_updater_status(db: Session):
    st.subheader("🔄 Background updater")
    latest = dict(
        db.query(UpdaterRun.job_name, func.max(UpdaterRun.finished_at))
        .group_by(UpdaterRun.job_name).all()
    )
    rows = []
    for job, label in _JOB_LABELS.items():
        finished = latest.get(job)
        run = None
        if finished is not None:
            run = db.query(UpdaterRun).filter(
                UpdaterRun.job_name == job, UpdaterRun.finished_at == finished
            ).first()
        last_ok = db.query(func.max(UpdaterRun.finished_at)).filter(
            UpdaterRun.job_name == job, UpdaterRun.success == True
        ).scalar()
        rows.append({
            "Job": label,
            "Last run": _to_display_tz(finished).strftime("%Y-%m-%d %H:%M") if finished else "Never (last 30 days)",
            "Result": ("✅ OK" if run.success else "❌ Failed") if run else "-",
            "Last success": _to_display_tz(last_ok).strftime("%Y-%m-%d %H:%M") if last_ok else "-",
            "Detail": (run.detail or "") if run else "",
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    st.caption(
        "Times are SAST. Missed runs (e.g. while the PC slept) are made up automatically "
        "once it wakes. If nothing has run recently, check the scheduled task with "
        "`schtasks /query /tn AlphaLensUpdater`."
    )


def _render_backups():
    st.subheader("💾 Database backups")
    backups = backup.list_backups()
    if backups:
        newest = backups[0]
        newest_at = backup.latest_backup_time()
        col1, col2, col3 = st.columns(3)
        col1.metric("Latest backup", newest_at.strftime("%Y-%m-%d %H:%M"))
        col2.metric("Copies kept", f"{len(backups)} / {settings.backup_keep}")
        col3.metric("Latest size", f"{newest.stat().st_size / 1024 / 1024:.1f} MB")
        if backup.backup_due():
            st.warning(
                f"The latest backup is more than {settings.backup_interval_days} days old - "
                "the updater should take one shortly. Check it is running."
            )
    else:
        st.warning("No backups yet. The updater takes one automatically; to take one now run "
                   "`python scripts\\run_updater.py --backup`.")
    st.caption(
        f"Stored in `{Path(settings.backup_dir).resolve()}` - one every "
        f"{settings.backup_interval_days} days. Restore steps are in the README (Backups)."
    )


def _render_data_sources():
    st.subheader("🌐 Data sources")
    quota = format_twelve_data_quota()
    rows = [
        {"Source": "Yahoo Finance", "Used for": "JSE prices, history, dividends, company search",
         "Status": "✅ On" if settings.yfinance_enabled else "— Off (YFINANCE_ENABLED)"},
        {"Source": "Twelve Data", "Used for": "Prices/history (free plan has no JSE coverage)",
         "Status": (f"✅ On - {quota}" if quota else "✅ On") if settings.twelve_data_enabled
         else "— Off (TWELVE_DATA_ENABLED)"},
        {"Source": "EasyEquities", "Used for": "Portfolio sync",
         "Status": _yes_no(bool(settings.easyequities_username and settings.easyequities_password))},
        {"Source": "Moneyweb", "Used for": "Top movers, dividend watch",
         "Status": _yes_no(bool(settings.moneyweb_username and settings.moneyweb_password))},
    ]
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
