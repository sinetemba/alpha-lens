import pandas as pd
import streamlit as st
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from datetime import datetime, timedelta, timezone
from loguru import logger

from app.models.dividend import Dividend, MoneywebDividendWatch
from app.models.portfolio import PortfolioHolding, is_jse_tracked_account
from app.models.stock import Stock, StockPrice
from app.models.watchlist import WatchlistItem
from app.services.data_service import DataService
from app.collectors.moneyweb import MoneywebCollector
from app.dashboard.utils import format_stock_label, render_csv_download, style_price_columns, render_last_fetch_caption, _to_display_tz
from app.dashboard.portfolio import EXCLUDED_ACCOUNT_TYPES


def show_dividends(db: Session):
    """Display the dividend tracking page."""
    st.header("Dividends")
    st.caption("Track dividend payments for your watchlist and portfolio.")

    # Summary metrics
    total_dividends = db.query(Dividend).count()
    # Summing per-share amounts across symbols is meaningless; count payers.
    paying_symbols = db.query(func.count(func.distinct(Dividend.symbol))).scalar() or 0

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

    # Income is shares-held-before-each-ex-date x amount (transaction log),
    # not current quantity x every dividend - top-ups/sells would misstate it.
    from app.services.portfolio_valuation import dividend_income_by_holding
    income_by_holding = dividend_income_by_holding(db, holdings)
    total_income = sum(income for income, _per_share in income_by_holding.values())

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Dividend Records", total_dividends)
    with col2:
        st.metric("Symbols Paying", paying_symbols)
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
        st.dataframe(data, width="stretch")
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
                # An explicit click forces a fetch - bypass the daily
                # freshness gate on Stock.last_dividend_fetch.
                saved = data_service.update_dividends(tracked, freshness_days=0)
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
        # Sub-R1 amounts keep up to 4 decimals (R 0.0525), others 2 (R 6.10).
        st.dataframe(
            style_price_columns(
                dividends_table.style, dividends_table, price_cols=("Amount (per share)",),
            ),
            width="stretch",
            hide_index=True,
            column_config={
                "Amount (per share)": st.column_config.NumberColumn("Amount (per share)"),
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
            income, per_share = income_by_holding.get(h.id, (0.0, 0.0))
            holdings_data.append({
                "Symbol": format_stock_label(h.symbol, h.stock.name if h.stock else None),
                "Account": h.account_type or "ZAR",
                "Quantity": h.quantity,
                "Dividends per Share": per_share,
                "Estimated Income": income,
            })

        income_table = pd.DataFrame(holdings_data)
        st.dataframe(
            style_price_columns(
                income_table.style, income_table, price_cols=("Dividends per Share",),
            ),
            width="stretch",
            hide_index=True,
            column_config={
                "Dividends per Share": st.column_config.NumberColumn("Dividends per Share"),
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
                    # Same cents-vs-Rand guards as the provider paths: the
                    # half-price check plus a ~100x-median check for small
                    # cent amounts on expensive shares.
                    stored_amount = amount
                    if (currency or "ZAR") == "ZAR":
                        closes = db.query(
                            StockPrice.timestamp, StockPrice.close_price
                        ).filter(
                            StockPrice.symbol == symbol,
                            StockPrice.close_price.isnot(None),
                            StockPrice.timestamp >= ex_dt - timedelta(days=730),
                            StockPrice.timestamp <= ex_dt + timedelta(days=730),
                        ).all()
                        stored_amount = DataService._dividend_amount_rand(
                            symbol, amount, DataService._close_near(closes, ex_dt),
                            [d.amount for d in db.query(Dividend.amount).filter(
                                Dividend.symbol == symbol,
                            ).all()],
                        )
                    dividend = Dividend(
                        stock_id=stock.id,
                        symbol=symbol,
                        amount=stored_amount,
                        currency=currency or "ZAR",
                        ex_dividend_date=ex_dt,
                        payment_date=datetime.combine(payment_date, datetime.min.time()) if payment_date else None,
                        frequency=frequency,
                    )
                    db.add(dividend)
                    try:
                        db.commit()
                    except IntegrityError:
                        # The daily job inserted the same (symbol, ex-date,
                        # amount) between our check and this commit.
                        db.rollback()
                        st.warning(f"A dividend for {symbol} on {ex_date} already exists.")
                    else:
                        # The cents->Rand conversion note must ride the flash -
                        # a same-run st.info dies with the st.rerun().
                        message = f"Added {symbol} dividend of R {stored_amount:.4f} per share."
                        if stored_amount != amount:
                            message += (
                                f" Entered {amount} looked like cents, so it "
                                "was converted."
                            )
                        st.session_state["dividends_fetch_flash"] = ("success", message)
                        st.rerun()
