import streamlit as st
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.dividend import Dividend
from app.models.portfolio import PortfolioHolding
from app.models.stock import Stock
from app.services.data_service import DataService
from app.dashboard.utils import format_stock_label


def show_dividends(db: Session):
    """Display the dividend tracking page."""
    st.header("Dividends")
    st.caption("Track dividend payments for your watchlist and portfolio.")

    # Summary metrics
    total_dividends = db.query(Dividend).count()
    total_amount = db.query(func.sum(Dividend.amount)).scalar() or 0.0

    holdings = db.query(PortfolioHolding).all()
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
                st.success(f"Saved {saved} new dividend records.")
            except Exception as e:
                st.error(f"Error fetching dividends: {e}")
        st.rerun()

    # All dividends table
    dividends = db.query(Dividend).order_by(Dividend.ex_dividend_date.desc()).all()
    if not dividends:
        st.info("No dividend records found. Click 'Fetch Latest Dividends' to populate.")
        return

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

    # Portfolio income by holding
    if holdings:
        st.subheader("Dividend Income by Holding")
        income_data = []
        for h in holdings:
            per_share = (
                db.query(func.sum(Dividend.amount))
                .filter(Dividend.symbol == h.symbol)
                .scalar()
                or 0.0
            )
            income = (h.quantity or 0.0) * per_share
            if per_share > 0:
                income_data.append({
                    "Symbol": format_stock_label(h.symbol, h.stock.name if h.stock else None),
                    "Quantity": h.quantity,
                    "Dividends per Share": f"R {per_share:,.4f}",
                    "Estimated Income": f"R {income:,.2f}",
                })

        if income_data:
            st.dataframe(income_data, use_container_width=True)
        else:
            st.info("No dividend income data for current holdings.")
