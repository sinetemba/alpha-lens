import streamlit as st
from sqlalchemy.orm import Session

from app.config.settings import settings
from app.models.base import utcnow_naive
from app.models.notification import Notification
from app.dashboard.utils import _to_display_tz, render_updater_status_banner

# Alerts the updater can raise - icons only, matching is by stored type.
_TYPE_ICONS = {
    "price": "💹",
    "technical": "📊",
    "news": "📰",
    "dividend": "💰",
    "portfolio": "💼",
    "ipo": "🚀",
    "sens": "📢",
    "system": "🔧",
}

_TYPE_LABELS = {
    "sens": "SENS", "price": "Price", "technical": "Technical", "news": "News",
    "dividend": "Dividend", "portfolio": "Portfolio", "ipo": "IPO", "system": "System",
}
# SENS alerts that may need a decision (offers, delisting, circulars...) are
# titled this way by services/holding_sens.py - they're pinned on top.
ACTION_PREFIX = "Action may be needed"

_PAGE_SIZE = 50


def _action_needed(n) -> bool:
    return (n.title or "").startswith(ACTION_PREFIX)


def show_notifications(db: Session):
    """Display the notifications page: alerts raised by the updater."""
    st.header("🔔 Notifications")
    st.caption("Alerts raised by the updater - SENS for your shares, prices, technicals, IPOs and system notices.")
    render_updater_status_banner(db)

    if not settings.notification_enabled:
        st.warning(
            "External delivery is disabled (NOTIFICATION_ENABLED=false). Alerts still "
            "appear here, but nothing is emailed or pushed. Set NOTIFICATION_ENABLED=true "
            "and configure a channel (email/ntfy/Discord/Telegram) in .env to enable delivery."
        )

    unread_count = db.query(Notification).filter(Notification.is_read == False).count()

    col1, col2, col3 = st.columns([1, 1, 2])
    with col1:
        st.metric("Unread", unread_count)
    with col2:
        if st.button("✅ Mark all as read", disabled=unread_count == 0):
            db.query(Notification).filter(Notification.is_read == False).update({
                "is_read": True,
                "read_at": utcnow_naive(),
            })
            db.commit()
            st.rerun()
    with col3:
        filter_choice = st.radio(
            "Show",
            ["Unread", "All"],
            horizontal=True,
            key="notifications_filter",
        )

    # Type filter - 17 unread SENS mixed routine notices (director dealings,
    # interest payments) with the two that needed a decision (Nov 2026).
    types_present = [t for (t,) in db.query(Notification.type).distinct().all() if t]
    type_options = sorted(types_present, key=lambda t: list(_TYPE_LABELS).index(t) if t in _TYPE_LABELS else 99)
    chosen_types = st.multiselect(
        "Types",
        type_options,
        default=type_options,
        format_func=lambda t: f"{_TYPE_ICONS.get(t, '🔔')} {_TYPE_LABELS.get(t, t)}",
        key="notifications_types",
    )

    st.markdown("---")

    query = db.query(Notification)
    if filter_choice == "Unread":
        query = query.filter(Notification.is_read == False)
    if chosen_types != type_options:
        query = query.filter(Notification.type.in_(chosen_types))

    total = query.count()
    notifications = (
        query.order_by(Notification.created_at.desc()).limit(_PAGE_SIZE).all()
    )

    if not notifications:
        st.info("No notifications match." if filter_choice == "All" or chosen_types != type_options
                else "No unread notifications.")
        return

    # Decision-type alerts first, then everything else newest-first.
    pinned = [n for n in notifications if _action_needed(n) and not n.is_read]
    if pinned:
        st.markdown(f"#### ⚠️ May need a decision ({len(pinned)})")
        for n in pinned:
            _render_notification(db, n)
        st.markdown("#### Everything else")
    rest = [n for n in notifications if n not in pinned]

    if total > _PAGE_SIZE:
        st.caption(f"Showing the {_PAGE_SIZE} most recent of {total} notifications.")

    for n in rest:
        _render_notification(db, n)


def _render_notification(db: Session, n) -> None:
    """One notification as an expander (decision-type ones open by default)."""
    title = n.title or "Notification"
    direction = "📈" if "increase" in title.lower() else ("📉" if "decrease" in title.lower() else None)
    icon = direction or _TYPE_ICONS.get(n.type, "🔔")
    read_marker = "🔵" if not n.is_read else "⚪"
    when = _to_display_tz(n.created_at).strftime("%d %b %Y %H:%M") if n.created_at else ""
    symbol = f" · {n.stock_symbol}" if n.stock_symbol else ""

    with st.expander(f"{read_marker} {icon} {title}{symbol} — {when}", expanded=_action_needed(n) and not n.is_read):
        st.markdown(n.message or "")
        meta = [f"type: {n.type or 'unknown'}"]
        if n.is_sent:
            sent_when = _to_display_tz(n.sent_at).strftime("%d %b %H:%M") if n.sent_at else ""
            meta.append(f"sent via {n.delivery_channel or 'all'} at {sent_when}")
        elif (n.attempts or 0) > 0:
            meta.append(f"send failed {n.attempts}x")
        else:
            meta.append("not sent yet")
        if n.is_read and n.read_at:
            meta.append(f"read {_to_display_tz(n.read_at).strftime('%d %b %H:%M')}")
        st.caption(" · ".join(meta))

        if not n.is_read:
            if st.button("Mark as read", key=f"mark_read_{n.id}"):
                n.is_read = True
                n.read_at = utcnow_naive()
                db.commit()
                st.rerun()
