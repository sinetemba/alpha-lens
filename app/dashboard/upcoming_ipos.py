"""Upcoming JSE IPOs / new listings, spotted by the updater's daily SENS scan."""
import pandas as pd
import streamlit as st
from sqlalchemy.orm import Session

from app.dashboard.utils import _to_display_tz
from app.models.upcoming_listing import UpcomingListing


def show_upcoming_ipos(db: Session):
    st.header("🚀 Upcoming JSE Listings")
    st.caption(
        "IPO announcements spotted in Moneyweb SENS. The headless updater scans "
        "daily (~08:00); subscribing still happens through your broker during "
        "the offer window in the pre-listing statement."
    )

    if st.button("🔄 Scan SENS now"):
        with st.spinner("Scanning Moneyweb SENS..."):
            from app.services.ipo import update_ipo_listings
            added = update_ipo_listings(db)
        if added:
            st.session_state["ipo_flash"] = f"Found {added} new IPO announcement(s)."
        st.rerun()
    flash = st.session_state.pop("ipo_flash", None)
    if flash:
        st.success(flash)

    rows = db.query(UpcomingListing).order_by(
        UpcomingListing.sens_published_at.desc().nullslast(),
        UpcomingListing.id.desc(),
    ).all()
    if not rows:
        st.info(
            "Nothing spotted yet. Genuine company IPOs are rare (~5-10 a year) - "
            "the page fills in when SENS shows an IPO-shaped announcement."
        )
        return

    announced = [r for r in rows if r.status == "announced"]
    listed = [r for r in rows if r.status == "listed"]
    other = [r for r in rows if r.status not in ("announced", "listed")]

    if announced:
        st.subheader("Announced / upcoming")
        st.dataframe(_table(announced), width="stretch", hide_index=True)

    if listed:
        st.subheader("Now trading")
        st.dataframe(_table(listed), width="stretch", hide_index=True)

    if other:
        with st.expander(f"Not new listings / expired ({len(other)})"):
            st.caption(
                "\"Already listed\": the ticker was trading before the "
                "announcement (a listed company's capital raise). \"Expired\": "
                "no trading within 180 days of the announcement."
            )
            st.dataframe(_table(other, with_status=True), width="stretch", hide_index=True)


_STATUS_LABELS = {"existing": "Already listed", "expired": "Expired"}


def _table(rows: list, with_status: bool = False) -> pd.DataFrame:
    df = pd.DataFrame([{
        "Announced": (
            _to_display_tz(r.sens_published_at).strftime("%Y-%m-%d %H:%M")
            if r.sens_published_at else ""
        ),
        "Ticker": r.symbol or "",
        "Company": r.name or "",
        "Announcement": r.headline or "",
        "Link": r.source_url or "",
        "Status": _STATUS_LABELS.get(r.status, r.status or ""),
    } for r in rows])
    return df if with_status else df.drop(columns=["Status"])
