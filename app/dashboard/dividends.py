import streamlit as st
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timezone

from app.models.dividend import Dividend
from app.models.portfolio import PortfolioHolding
from app.models.stock import Stock
from app.services.data_service import DataService
from app.dashboard.utils import format_stock_label
from app.dashboard.portfolio import EXCLUDED_ACCOUNT_TYPES


def show_dividends(db: Session):
    """Display the dividend tracking page."""
    st.header("Dividends")
    st.caption("Track dividend payments for your watchlist and portfolio.")

    # Summary metrics
    total_dividends = db.query(Dividend).count()
    total_amount = db.query(func.sum(Dividend.amount)).scalar() or 0.0

    holdings = db.query(PortfolioHolding).filter(
        ~PortfolioHolding.account_type.in_(EXCLUDED_ACCOUNT_TYPES)
    ).all()
    total_income = 0.0
    for h in holdings:
        per_share = (
            db.query(func.sum(Dividend.amount))
            .filter(Dividend.symbol == h.symbol)
            .scalar()
            or 0.0
        )
        total_income += (h.quantity or 0.0) * per_share

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Dividend Records", total_dividends)
    with col2:
        st.metric("Total Dividends (per share)", f"R {total_amount:,.4f}")
    with col3:
        st.metric("Portfolio Dividend Income", f"R {total_income:,.2f}")

    st.markdown("---")

    # Fetch control
    if st.button("Fetch Latest Dividends"):
        with st.spinner("Fetching dividend history from Yahoo Finance..."):
            try:
                data_service = DataService(db)
                saved = data_service.update_dividends()
                if saved > 0:
                    st.success(f"Saved {saved} new dividend records.")
                else:
                    st.warning(
                        "No new dividend records found. This can happen if Yahoo Finance "
                        "has no data for the active symbols, rate-limits the request, or "
                        "if no stocks are marked active. Try adding a dividend manually below."
                    )
            except Exception as e:
                st.error(f"Error fetching dividends: {e}")
        st.rerun()

    # All dividends table
    dividends = db.query(Dividend).order_by(Dividend.ex_dividend_date.desc()).all()
    if dividends:
        data = []
        for d in dividends:
            data.append({
                "Symbol": format_stock_label(d.symbol, d.stock.name if d.stock else None),
                "Amount (per share)": f"R {d.amount:,.4f}",
                "Ex-Dividend Date": d.ex_dividend_date.strftime("%Y-%m-%d") if d.ex_dividend_date else "N/A",
                "Payment Date": d.payment_date.strftime("%Y-%m-%d") if d.payment_date else "N/A",
                "Frequency": d.frequency or "N/A",
                "Currency": d.currency or "ZAR",
            })

        st.subheader("All Dividends")
        st.dataframe(data, use_container_width=True)
    else:
        st.info("No dividend records found yet. Fetch from Yahoo Finance or add one manually below.")

    # All holdings with dividend income
    if holdings:
        st.subheader("All Holdings")
        holdings_data = []
        for h in holdings:
            per_share = (
                db.query(func.sum(Dividend.amount))
                .filter(Dividend.symbol == h.symbol)
                .scalar()
                or 0.0
            )
            income = (h.quantity or 0.0) * per_share
            holdings_data.append({
                "Symbol": format_stock_label(h.symbol, h.stock.name if h.stock else None),
                "Account": h.account_type or "ZAR",
                "Quantity": h.quantity,
                "Dividends per Share": f"R {per_share:,.4f}",
                "Estimated Income": f"R {income:,.2f}",
            })

        st.dataframe(holdings_data, use_container_width=True)
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
            ex_date = st.date_input("Ex-Dividend Date", value=datetime.now(timezone.utc).date())
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

                dividend = Dividend(
                    stock_id=stock.id,
                    symbol=symbol,
                    amount=amount,
                    currency=currency or "ZAR",
                    ex_dividend_date=datetime.combine(ex_date, datetime.min.time()),
                    payment_date=datetime.combine(payment_date, datetime.min.time()) if payment_date else None,
                    frequency=frequency,
                )
                db.add(dividend)
                db.commit()
                st.success(f"Added {symbol} dividend of R {amount:.4f} per share.")
                st.rerun()
