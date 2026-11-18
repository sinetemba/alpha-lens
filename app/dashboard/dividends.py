import pandas as pd
import streamlit as st
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from datetime import datetime, timedelta, timezone
from loguru import logger

from app.models.dividend import Dividend, MoneywebDividendWatch
from app.models.portfolio import PortfolioHolding, is_jse_tracked_account
from app.models.stock import Stock
from app.models.watchlist import WatchlistItem
from app.services.data_service import DataService
from app.collectors.moneyweb import MoneywebCollector
from app.dashboard.utils import format_stock_label, render_csv_download, render_last_fetch_caption, _to_display_tz
from app.dashboard.portfolio import EXCLUDED_ACCOUNT_TYPES


def show_dividends(db: Session):
    """Display the dividend tracking page."""
    st.header("Dividends")
    st.caption("Track dividend payments for your watchlist and portfolio.")

    # Summary metrics
    total_dividends = db.query(Dividend).count()
    total_amount = db.query(func.sum(Dividend.amount)).scalar() or 0.0

    holdings = [
        h for h in db.query(PortfolioHolding).options(
            joinedload(PortfolioHolding.stock)
        ).filter(
            ~PortfolioHolding.account_type.in_(EXCLUDED_ACCOUNT_TYPES)
        ).all()
        # Dividend records are JSE-only; attributing them to a USD holding
        # whose bare ticker happens to collide with a JSE symbol would
        # fabricate income and mix currencies.
        if is_jse_tracked_account(h.account_type)
    ]

    # Only dividends with ex-date on/after the holding's purchase date were
    # actually received - earlier ones overstate income. One query fetches the
    # per-symbol rows, then ex-dates are filtered per holding (account-type
    # purchase dates differ).
    dividends_by_symbol: dict[str, list] = {}
    if holdings:
        for sym, amount, ex_date in db.query(
            Dividend.symbol, Dividend.amount, Dividend.ex_dividend_date
        ).filter(Dividend.symbol.in_({h.symbol for h in holdings})).all():
            dividends_by_symbol.setdefault(sym, []).append((ex_date, amount or 0.0))

    def _holding_dividends_per_share(h) -> float:
        return sum(
            amount
            for ex_date, amount in dividends_by_symbol.get(h.symbol, [])
            if h.purchase_date is None or (ex_date is not None and ex_date >= h.purchase_date)
        )

    total_income = sum(
        (h.quantity or 0.0) * _holding_dividends_per_share(h) for h in holdings
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Dividend Records", total_dividends)
    with col2:
        st.metric("Total Dividends (per share)", f"R {total_amount:,.4f}")
    with col3:
        st.metric("Dividend Income (since purchase)", f"R {total_income:,.2f}")

    st.markdown("---")
    render_last_fetch_caption(db, [h.symbol for h in holdings])

    # Moneyweb Dividend Watch Calendar
    st.subheader("Dividends Watch Calendar")

    # Fetch results are flashed after the rerun below - messages rendered in
    # the same run as st.rerun() are discarded before the user sees them.
    flash = st.session_state.pop("dividends_fetch_flash", None)
    if flash:
        level, message = flash
        getattr(st, level)(message)

    if st.button("🌐 Fetch from Moneyweb"):
        with st.spinner("Pulling dividend watch data from Moneyweb..."):
            collector = MoneywebCollector()
            saved = collector.save_dividend_watch(db)
        if saved > 0:
            st.session_state["dividends_fetch_flash"] = (
                "success", f"Saved {saved} dividend calendar rows from Moneyweb."
            )
        elif collector.last_error:
            st.session_state["dividends_fetch_flash"] = (
                "warning", f"Could not fetch Moneyweb data: {collector.last_error}"
            )
        else:
            st.session_state["dividends_fetch_flash"] = (
                "info", "No new Moneyweb dividend data found."
            )
        st.rerun()

    moneyweb_rows = db.query(MoneywebDividendWatch).order_by(
        MoneywebDividendWatch.pay_date
    ).all()

    if moneyweb_rows:
        latest = db.query(MoneywebDividendWatch).order_by(
            MoneywebDividendWatch.fetched_at.desc()
        ).first()
        if latest and latest.fetched_at:
            st.caption(f"Last fetched: {_to_display_tz(latest.fetched_at).strftime('%Y-%m-%d %H:%M')} SAST")

        data = []
        for d in moneyweb_rows:
            data.append({
                "Instrument": d.instrument,
                "Declared date": d.declared_date.strftime("%Y/%m/%d") if d.declared_date else "N/A",
                "Last day to trade": d.last_day_to_trade.strftime("%Y/%m/%d") if d.last_day_to_trade else "N/A",
                "Pay date": d.pay_date.strftime("%Y/%m/%d") if d.pay_date else "N/A",
                "Type": d.dividend_type or "N/A",
                "Value": d.value or "N/A",
            })
        st.dataframe(data, use_container_width=True)
    else:
        st.info("No Moneyweb Dividend Watch data yet. Click 'Fetch from Moneyweb' to load it.")

    st.markdown("---")

    # Fetch control
    if st.button("Fetch Latest Dividends"):
        with st.spinner("Fetching dividend history from Yahoo Finance..."):
            try:
                data_service = DataService(db)
                # Restrict to tracked symbols - fetching for every active
                # stock costs ~1 Twelve Data call per symbol (~72 calls).
                tracked = sorted(
                    {h.symbol for h in holdings}
                    | {
                        item.symbol
                        for item in db.query(WatchlistItem)
                        .filter(WatchlistItem.is_active == True)
                        .all()
                    }
                )
                saved = data_service.update_dividends(tracked)
                if saved > 0:
                    st.session_state["dividends_fetch_flash"] = (
                        "success", f"Saved {saved} new dividend records."
                    )
                else:
                    st.session_state["dividends_fetch_flash"] = (
                        "warning",
                        "No new dividend records found. This can happen if Yahoo Finance "
                        "has no data for the active symbols, rate-limits the request, or "
                        "if no stocks are marked active. Try adding a dividend manually below.",
                    )
            except Exception as e:
                logger.error(f"Error fetching dividends: {e}")
                st.session_state["dividends_fetch_flash"] = (
                    "error", "Failed to fetch dividends - see logs for details."
                )
        st.rerun()

    # All dividends table (most recent first, capped so the page stays light)
    dividends = db.query(Dividend).options(joinedload(Dividend.stock)).order_by(
        Dividend.ex_dividend_date.desc()
    ).limit(200).all()
    if len(dividends) >= 200 and total_dividends > len(dividends):
        st.caption(f"Showing the {len(dividends)} most recent of {total_dividends} dividend records.")
    if dividends:
        data = []
        for d in dividends:
            data.append({
                "Symbol": format_stock_label(d.symbol, d.stock.name if d.stock else None),
                "Amount (per share)": d.amount,
                "Ex-Dividend Date": d.ex_dividend_date.strftime("%Y-%m-%d") if d.ex_dividend_date else "N/A",
                "Payment Date": d.payment_date.strftime("%Y-%m-%d") if d.payment_date else "N/A",
                "Frequency": d.frequency or "N/A",
                "Currency": d.currency or "ZAR",
            })

        st.subheader("All Dividends")
        dividends_table = pd.DataFrame(data)
        st.dataframe(
            dividends_table,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Amount (per share)": st.column_config.NumberColumn("Amount (per share)", format="R %.4f"),
            },
        )
        render_csv_download(dividends_table, "dividends", key="dividends_csv")
    else:
        st.info("No dividend records found yet. Fetch from Yahoo Finance or add one manually below.")

    # All holdings with dividend income
    if holdings:
        st.subheader("All Holdings")
        holdings_data = []
        for h in holdings:
            per_share = _holding_dividends_per_share(h)
            income = (h.quantity or 0.0) * per_share
            holdings_data.append({
                "Symbol": format_stock_label(h.symbol, h.stock.name if h.stock else None),
                "Account": h.account_type or "ZAR",
                "Quantity": h.quantity,
                "Dividends per Share": per_share,
                "Estimated Income": income,
            })

        income_table = pd.DataFrame(holdings_data)
        st.dataframe(
            income_table,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Dividends per Share": st.column_config.NumberColumn("Dividends per Share", format="R %.4f"),
                "Estimated Income": st.column_config.NumberColumn("Estimated Income", format="R %.2f"),
            },
        )
        render_csv_download(income_table, "dividend_income", key="dividend_income_csv")
    else:
        st.info("No holdings found. Add holdings on the Portfolio page first.")

    # Manual entry
    st.markdown("---")
    with st.expander("➕ Add Dividend Manually"):
        col1, col2 = st.columns(2)
        with col1:
            symbol = st.text_input("Symbol", placeholder="e.g. NPN").upper().strip()
            amount = st.number_input("Amount per Share", min_value=0.0, step=0.01)
            currency = st.text_input("Currency", value="ZAR").upper().strip()
        with col2:
            ex_date = st.date_input("Ex-Dividend Date", value=_to_display_tz(datetime.now(timezone.utc)).date())
            payment_date = st.date_input("Payment Date (optional)", value=None)
            frequency = st.selectbox(
                "Frequency",
                [None, "monthly", "quarterly", "semi-annual", "annual"],
                format_func=lambda x: "Select..." if x is None else x,
            )

        if st.button("Add Dividend"):
            if not symbol or amount <= 0 or not ex_date:
                st.error("Symbol, amount and ex-dividend date are required.")
            else:
                stock = db.query(Stock).filter(Stock.symbol == symbol).first()
                if not stock:
                    stock = Stock(symbol=symbol, name=symbol)
                    db.add(stock)
                    db.flush()

                ex_dt = datetime.combine(ex_date, datetime.min.time())
                # Match on calendar day + amount: a special dividend can
                # share an ex-date with an interim, so day-only matching
                # would wrongly block it (the unique index is 3-column too).
                same_day = db.query(Dividend).filter(
                    Dividend.symbol == symbol,
                    Dividend.ex_dividend_date >= ex_dt,
                    Dividend.ex_dividend_date < ex_dt + timedelta(days=1),
                ).all()
                if any(abs((d.amount or 0.0) - amount) < 1e-6 for d in same_day):
                    st.warning(f"A dividend for {symbol} on {ex_date} already exists.")
                else:
                    dividend = Dividend(
                        stock_id=stock.id,
                        symbol=symbol,
                        amount=amount,
                        currency=currency or "ZAR",
                        ex_dividend_date=ex_dt,
                        payment_date=datetime.combine(payment_date, datetime.min.time()) if payment_date else None,
                        frequency=frequency,
                    )
                    db.add(dividend)
                    db.commit()
                    st.success(f"Added {symbol} dividend of R {amount:.4f} per share.")
                    st.rerun()
