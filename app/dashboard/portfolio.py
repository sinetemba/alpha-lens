import pandas as pd
import streamlit as st
from sqlalchemy.orm import Session
from app.models.portfolio import Portfolio, PortfolioHolding
from app.models.stock import Stock, StockPrice
from app.dashboard.utils import format_stock_label
from app.collectors.easyequities import EasyEquitiesCollector
from datetime import datetime
from loguru import logger


EXCLUDED_PORTFOLIO_SYMBOLS = {"SZK"}


def show_portfolio(db: Session):
    """Display the portfolio page."""
    st.header("💼 Portfolio")
    st.markdown("Track your investment portfolio and performance.")
    
    # Get or create portfolio
    portfolio = db.query(Portfolio).first()
    
    if not portfolio:
        portfolio = Portfolio(name="My Portfolio")
        db.add(portfolio)
        db.commit()
        st.info("Created new portfolio. Add holdings below.")

    collector = EasyEquitiesCollector()
    holdings = db.query(PortfolioHolding).filter(
        PortfolioHolding.portfolio_id == portfolio.id,
        ~PortfolioHolding.symbol.in_(EXCLUDED_PORTFOLIO_SYMBOLS),
    ).order_by(PortfolioHolding.purchase_date.asc()).all()
    refresh_requested, sync_caption = _render_easyequities_refresh_control(collector, holdings)
    synced_holdings = None
    if collector.enabled:
        spinner_message = "Refreshing portfolio from EasyEquities..." if refresh_requested else "Syncing portfolio from EasyEquities..."
        with st.spinner(spinner_message):
            synced_holdings = _sync_portfolio_from_easyequities(db, portfolio.id, collector)
        if synced_holdings is None:
            st.warning("EasyEquities sync failed. Showing the most recent database values.")
        elif refresh_requested:
            st.success(f"Synced {synced_holdings} holding{'s' if synced_holdings != 1 else ''} from EasyEquities.")
    else:
        st.info("Set EASYEQUITIES_USERNAME and EASYEQUITIES_PASSWORD in .env to enable live portfolio sync.")

    holdings = db.query(PortfolioHolding).filter(
        PortfolioHolding.portfolio_id == portfolio.id,
        ~PortfolioHolding.symbol.in_(EXCLUDED_PORTFOLIO_SYMBOLS),
    ).order_by(PortfolioHolding.purchase_date.asc()).all()
    _update_easyequities_sync_caption(sync_caption, holdings)
    if synced_holdings is None:
        for holding in holdings:
            _update_holding_values(db, holding)
    _update_portfolio_totals(db, portfolio, holdings)

    # Portfolio summary
    total_purchase_value = sum(holding.quantity * holding.purchase_price for holding in holdings)
    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        st.metric("Total Value", f"R {portfolio.current_value:,.2f}")

    with col2:
        st.metric("Total Purchase Value", f"R {total_purchase_value:,.2f}")

    with col3:
        st.metric("Total Gain/Loss", f"R {portfolio.total_gain_loss:,.2f}")

    with col4:
        st.metric("Gain/Loss %", f"{portfolio.total_gain_loss_percentage:.2f}%")

    with col5:
        st.metric("Holdings", len(holdings))
    
    st.markdown("---")
    
    # Add new holding
    with st.expander("➕ Add Holding"):
        col1, col2 = st.columns(2)
        
        with col1:
            symbol = st.text_input("Symbol", placeholder="e.g., NPN").upper()
            quantity = st.number_input("Quantity", min_value=0.0, step=1.0)
        
        with col2:
            purchase_price = st.number_input("Purchase Price", min_value=0.0, step=0.01)
            purchase_date = st.date_input("Purchase Date", value=datetime.now().date())
        
        if st.button("Add Holding"):
            if symbol and quantity > 0 and purchase_price > 0:
                _add_holding(db, portfolio.id, symbol, quantity, purchase_price, purchase_date)
                st.success(f"Added {quantity} shares of {symbol} to portfolio!")
                st.rerun()
            else:
                st.error("Please fill in all required fields.")
    
    st.markdown("---")

    # Sync holdings from EasyEquities
    with st.expander("🔄 Sync from EasyEquities"):
        st.caption(
            "EasyEquities has no official public API. This uses an unofficial client that "
            "logs in with your EasyEquities credentials (configured via EASYEQUITIES_USERNAME "
            "/ EASYEQUITIES_PASSWORD in .env) to read your real holdings."
        )

        collector = EasyEquitiesCollector()

        if not collector.enabled:
            st.info("Set EASYEQUITIES_USERNAME and EASYEQUITIES_PASSWORD in .env to enable this.")
        else:
            if st.button("Fetch EasyEquities Accounts"):
                with st.spinner("Logging in to EasyEquities..."):
                    accounts = collector.list_accounts()

                if accounts:
                    st.session_state["ee_accounts"] = accounts
                else:
                    st.error("Could not fetch accounts. Check your EasyEquities credentials.")
                    st.session_state.pop("ee_accounts", None)

            accounts = st.session_state.get("ee_accounts")
            if accounts:
                account_labels = {a["id"]: f"{a['name']} ({a['trading_currency_id']})" for a in accounts}
                account_id = st.selectbox(
                    "Account",
                    list(account_labels.keys()),
                    format_func=lambda aid: account_labels[aid]
                )

                if st.button("Sync Holdings"):
                    with st.spinner("Fetching holdings from EasyEquities..."):
                        ee_holdings = collector.get_holdings(account_id)

                    if ee_holdings is None:
                        st.error("Could not fetch holdings from EasyEquities.")
                    elif not ee_holdings:
                        st.info("No holdings found in this EasyEquities account.")
                    else:
                        for ee_holding in ee_holdings:
                            _sync_holding_from_easyequities(db, portfolio.id, ee_holding)
                        st.success(f"Synced {len(ee_holdings)} holdings from EasyEquities.")
                        st.rerun()

    st.markdown("---")

    # Holdings table
    st.subheader("Portfolio Holdings")

    if not holdings:
        st.info("No holdings in portfolio. Add holdings above.")
        return

    winning_holdings = sum(holding.gain_loss is not None and holding.gain_loss > 0 for holding in holdings)
    losing_holdings = sum(holding.gain_loss is not None and holding.gain_loss < 0 for holding in holdings)
    unchanged_holdings = len(holdings) - winning_holdings - losing_holdings
    summary = f"🟢 {winning_holdings} winning | 🔴 {losing_holdings} losing"
    if unchanged_holdings:
        summary += f" | 🟡 {unchanged_holdings} unchanged"
    st.caption(summary)

    data = []
    for holding in holdings:
        data.append({
            "Symbol": format_stock_label(holding.symbol, holding.stock.name if holding.stock else None),
            "Quantity": holding.quantity,
            "Purchase Price": f"R {holding.purchase_price:.2f}",
            "Current Price": f"R {holding.current_price:.2f}" if holding.current_price else "N/A",
            "Total Purchase Value": f"R {holding.quantity * holding.purchase_price:.2f}",
            "Current Value": f"R {holding.current_value:.2f}" if holding.current_value else "N/A",
            "Gain/Loss": f"R {holding.gain_loss:.2f}" if holding.gain_loss else "N/A",
            "Gain/Loss %": f"{holding.gain_loss_percentage:.2f}%" if holding.gain_loss_percentage else "N/A",
            "Dividends": f"R {holding.total_dividends_received:.2f}",
            "_gain_loss": holding.gain_loss,
        })
    
    if data:
        holdings_table = pd.DataFrame(data)
        available_columns = [column for column in holdings_table.columns if column != "_gain_loss"]
        selected_columns = st.multiselect(
            "Columns to display",
            available_columns,
            default=available_columns,
            key="portfolio_holding_columns",
        )
        if selected_columns:
            visible_columns = [column for column in available_columns if column in selected_columns]
            visible_table = holdings_table[visible_columns + ["_gain_loss"]]
            styled_table = visible_table.style.apply(_style_holding_gain_loss, axis=1).hide(
                axis="columns", subset=["_gain_loss"]
            )
            st.dataframe(
                styled_table,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Symbol": st.column_config.TextColumn("Symbol", help="Ticker symbol and company name."),
                    "Quantity": st.column_config.NumberColumn("Quantity", help="Number of shares currently held."),
                    "Purchase Price": st.column_config.TextColumn("Purchase Price", help="Average price paid per share."),
                    "Current Price": st.column_config.TextColumn("Current Price", help="Latest market price per share from EasyEquities."),
                    "Total Purchase Value": st.column_config.TextColumn("Total Purchase Value", help="Quantity multiplied by the average purchase price."),
                    "Current Value": st.column_config.TextColumn("Current Value", help="Current market value of the holding."),
                    "Gain/Loss": st.column_config.TextColumn("Gain/Loss", help="Current value less total purchase value."),
                    "Gain/Loss %": st.column_config.TextColumn("Gain/Loss %", help="Gain or loss as a percentage of total purchase value."),
                    "Dividends": st.column_config.TextColumn("Dividends", help="Total dividends recorded for this holding."),
                },
            )
        else:
            st.info("Select at least one column to display the holdings table.")
    
    st.markdown("---")
    
    # Remove holding
    st.subheader("Remove Holding")
    holding_symbols = [h.symbol for h in holdings]
    holding_names = {h.symbol: h.stock.name if h.stock else None for h in holdings}
    symbol_to_remove = st.selectbox(
        "Select holding to remove",
        holding_symbols,
        format_func=lambda s: format_stock_label(s, holding_names.get(s))
    )
    
    if st.button("Remove Holding"):
        _remove_holding(db, portfolio.id, symbol_to_remove)
        st.success(f"Removed {symbol_to_remove} from portfolio.")
        st.rerun()


def _style_holding_gain_loss(row: pd.Series) -> list[str]:
    gain_loss = row["_gain_loss"]
    if gain_loss is None:
        color = "#fef3c7"
    elif gain_loss > 0:
        color = "#dcfce7"
    elif gain_loss < 0:
        color = "#fee2e2"
    else:
        color = "#fef3c7"
    return [f"background-color: {color}"] * len(row)


def _render_easyequities_refresh_control(
    collector: EasyEquitiesCollector, holdings: list[PortfolioHolding]
) -> tuple[bool, object]:
    _, refresh_column = st.columns([3, 1])
    with refresh_column:
        refresh_requested = st.button(
            "🔄 Refresh Portfolio",
            key="portfolio_easyequities_refresh",
            disabled=not collector.enabled,
        )
        sync_caption = st.empty()
        _update_easyequities_sync_caption(sync_caption, holdings)
    return refresh_requested, sync_caption


def _update_easyequities_sync_caption(sync_caption, holdings: list[PortfolioHolding]) -> None:
    latest_update = max((holding.updated_at for holding in holdings if holding.updated_at), default=None)
    if latest_update:
        sync_caption.caption(f"Last EasyEquities sync: {latest_update.strftime('%d %b %Y %H:%M')}")
    else:
        sync_caption.caption("No EasyEquities sync recorded yet.")


def _sync_portfolio_from_easyequities(
    db: Session, portfolio_id: int, collector: EasyEquitiesCollector
) -> int | None:
    ee_holdings = collector.get_all_holdings()
    if ee_holdings is None:
        return None

    aggregated_holdings = {}
    for ee_holding in ee_holdings:
        symbol = ee_holding["symbol"]
        if not symbol or symbol in EXCLUDED_PORTFOLIO_SYMBOLS or ee_holding["shares"] <= 0:
            continue
        current = aggregated_holdings.setdefault(symbol, {
            "symbol": symbol,
            "name": ee_holding.get("name") or symbol,
            "shares": 0.0,
            "purchase_value": 0.0,
            "current_value": 0.0,
        })
        current["shares"] += ee_holding["shares"]
        current["purchase_value"] += ee_holding["purchase_value"]
        current["current_value"] += ee_holding["current_value"]

    for ee_holding in aggregated_holdings.values():
        ee_holding["purchase_price"] = ee_holding["purchase_value"] / ee_holding["shares"]
        ee_holding["current_price"] = ee_holding["current_value"] / ee_holding["shares"]
        _sync_holding_from_easyequities(db, portfolio_id, ee_holding)

    return len(aggregated_holdings)


def _add_holding(db: Session, portfolio_id: int, symbol: str, quantity: float, purchase_price: float, purchase_date):
    """Add a holding to the portfolio."""
    if symbol in EXCLUDED_PORTFOLIO_SYMBOLS:
        return

    # Check if stock exists
    stock = db.query(Stock).filter(Stock.symbol == symbol).first()
    
    if not stock:
        from app.services.data_service import get_company_name
        stock = Stock(symbol=symbol, name=get_company_name(symbol))
        db.add(stock)
        db.flush()
    
    # Create holding
    holding = PortfolioHolding(
        portfolio_id=portfolio_id,
        stock_id=stock.id,
        symbol=symbol,
        quantity=quantity,
        purchase_price=purchase_price,
        purchase_date=purchase_date
    )
    
    db.add(holding)
    db.commit()

    # Backfill historical prices and technical indicators for the newly added stock
    try:
        from app.services.data_service import DataService
        with st.spinner(f"Fetching price history for {symbol}..."):
            data_service = DataService(db)
            saved = data_service.update_historical_data(symbol)
        logger.info(f"Fetched {saved} price points for {symbol}")
    except Exception as e:
        logger.error(f"Failed to fetch price data for {symbol}: {e}")


def _sync_holding_from_easyequities(db: Session, portfolio_id: int, ee_holding: dict):
    """Create or update a holding from parsed EasyEquities data."""
    symbol = ee_holding["symbol"]
    if not symbol or symbol in EXCLUDED_PORTFOLIO_SYMBOLS or ee_holding["shares"] <= 0:
        return

    stock = db.query(Stock).filter(Stock.symbol == symbol).first()
    if not stock:
        stock = Stock(symbol=symbol, name=ee_holding.get("name") or symbol)
        db.add(stock)
        db.flush()

    holding = db.query(PortfolioHolding).filter(
        PortfolioHolding.portfolio_id == portfolio_id,
        PortfolioHolding.symbol == symbol
    ).first()

    is_new = holding is None
    if is_new:
        holding = PortfolioHolding(
            portfolio_id=portfolio_id,
            stock_id=stock.id,
            symbol=symbol,
            purchase_date=datetime.utcnow(),
        )
        db.add(holding)

    # EasyEquities is treated as the source of truth for quantity, cost basis, and market value on sync
    holding.quantity = ee_holding["shares"]
    holding.purchase_price = ee_holding["purchase_price"]
    holding.current_price = ee_holding["current_price"]
    holding.current_value = ee_holding["current_value"]
    holding.gain_loss = holding.current_value - (holding.quantity * holding.purchase_price)
    holding.gain_loss_percentage = (
        (holding.gain_loss / (holding.quantity * holding.purchase_price)) * 100
        if holding.purchase_price else 0.0
    )

    db.commit()


def _remove_holding(db: Session, portfolio_id: int, symbol: str):
    """Remove a holding from the portfolio."""
    db.query(PortfolioHolding).filter(
        PortfolioHolding.portfolio_id == portfolio_id,
        PortfolioHolding.symbol == symbol
    ).delete()
    db.commit()


def _update_holding_values(db: Session, holding: PortfolioHolding):
    """Update current values for a holding."""
    # Get latest price
    latest_price = db.query(StockPrice).filter(
        StockPrice.symbol == holding.symbol
    ).order_by(StockPrice.timestamp.desc()).first()
    
    if latest_price:
        holding.current_price = latest_price.close_price
        holding.current_value = holding.quantity * latest_price.close_price
        holding.gain_loss = holding.current_value - (holding.quantity * holding.purchase_price)
        holding.gain_loss_percentage = (holding.gain_loss / (holding.quantity * holding.purchase_price)) * 100
    else:
        holding.current_price = None
        holding.current_value = None
        holding.gain_loss = None
        holding.gain_loss_percentage = None

    db.commit()


def _update_portfolio_totals(db: Session, portfolio: Portfolio, holdings: list) -> None:
    """Recompute portfolio-level summary metrics from its current holdings."""
    total_value = sum(h.current_value or 0.0 for h in holdings)
    total_cost = sum(h.quantity * h.purchase_price for h in holdings)

    portfolio.current_value = total_value
    portfolio.total_gain_loss = total_value - total_cost
    portfolio.total_gain_loss_percentage = (
        (portfolio.total_gain_loss / total_cost) * 100 if total_cost else 0.0
    )

    db.commit()


def _get_price_movements(db: Session, symbol: str) -> tuple:
    """Calculate daily, weekly, and monthly price movements."""
    now = datetime.utcnow()
    
    # Get latest price
    latest = db.query(StockPrice).filter(
        StockPrice.symbol == symbol
    ).order_by(StockPrice.timestamp.desc()).first()
    
    if not latest:
        return 0.0, 0.0, 0.0
    
    latest_price = latest.close_price
    
    # Daily change (compare with previous trading day)
    daily_prev = db.query(StockPrice).filter(
        StockPrice.symbol == symbol,
        StockPrice.timestamp < latest.timestamp,
        StockPrice.timestamp >= now - timedelta(days=2)
    ).order_by(StockPrice.timestamp.desc()).first()
    
    daily_change = 0.0
    if daily_prev and daily_prev.close_price > 0:
        daily_change = ((latest_price - daily_prev.close_price) / daily_prev.close_price) * 100
    
    # Weekly change (compare with price 7 days ago)
    weekly_prev = db.query(StockPrice).filter(
        StockPrice.symbol == symbol,
        StockPrice.timestamp >= now - timedelta(days=8),
        StockPrice.timestamp <= now - timedelta(days=6)
    ).order_by(StockPrice.timestamp.asc()).first()
    
    weekly_change = 0.0
    if weekly_prev and weekly_prev.close_price > 0:
        weekly_change = ((latest_price - weekly_prev.close_price) / weekly_prev.close_price) * 100
    
    # Monthly change (compare with price 30 days ago)
    monthly_prev = db.query(StockPrice).filter(
        StockPrice.symbol == symbol,
        StockPrice.timestamp >= now - timedelta(days=32),
        StockPrice.timestamp <= now - timedelta(days=28)
    ).order_by(StockPrice.timestamp.asc()).first()
    
    monthly_change = 0.0
    if monthly_prev and monthly_prev.close_price > 0:
        monthly_change = ((latest_price - monthly_prev.close_price) / monthly_prev.close_price) * 100
    
    return daily_change, weekly_change, monthly_change
