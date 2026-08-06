import pandas as pd
import streamlit as st
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from app.models.stock import Stock
from app.models.watchlist import WatchlistItem
from app.models.portfolio import PortfolioHolding
from app.dashboard.utils import format_stock_label, render_market_data_refresh_control, style_gain_loss_row
from app.dashboard.home import _get_latest_and_previous_prices
from app.dashboard.portfolio import EXCLUDED_ACCOUNT_TYPES
from loguru import logger


def show_watchlist(db: Session):
    """Display the watchlist page."""
    st.header("📋 Watchlist")
    st.markdown("Manage your stock watchlist and track performance.")

    active_symbols = [
        item.symbol for item in db.query(WatchlistItem).filter(WatchlistItem.is_active == True).all()
    ]
    render_market_data_refresh_control(db, key="watchlist_market_data_refresh", symbols=active_symbols)
    
    # Add new stock to watchlist
    with st.expander("➕ Add Stock to Watchlist"):
        col1, col2 = st.columns(2)
        
        with col1:
            symbol = st.text_input("Symbol", placeholder="e.g., NPN").upper()
        
        with col2:
            target_price = st.number_input("Target Price", min_value=0.0, step=0.01)
        
        purchase_price = st.number_input("Purchase Price", min_value=0.0, step=0.01)
        stop_loss = st.number_input("Stop Loss", min_value=0.0, step=0.01)
        notes = st.text_area("Notes")
        
        if st.button("Add to Watchlist"):
            if symbol:
                _add_to_watchlist(db, symbol, target_price, purchase_price, stop_loss, notes)
                st.success(f"Added {symbol} to watchlist!")
                st.rerun()
            else:
                st.error("Please enter a stock symbol.")
    
    st.markdown("---")
    
    # Display watchlist (eager-load stock to avoid N+1 name lookups)
    watchlist_items = db.query(WatchlistItem).options(
        joinedload(WatchlistItem.stock)
    ).filter(
        WatchlistItem.is_active == True
    ).all()
    
    if not watchlist_items:
        st.info("No stocks in watchlist. Add stocks above to start tracking.")
        return
    
    # Summary metrics
    prices = _get_latest_and_previous_prices(db, [item.symbol for item in watchlist_items])

    above_target = 0
    below_stop = 0
    for item in watchlist_items:
        latest, _ = prices.get(item.symbol, (None, None))
        if latest and latest.close_price:
            if item.target_price and latest.close_price >= item.target_price:
                above_target += 1
            if item.stop_loss and latest.close_price <= item.stop_loss:
                below_stop += 1

    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.metric("Total Stocks", len(watchlist_items))
    
    with col2:
        st.metric("Above Target", above_target)
    
    with col3:
        st.metric("Below Stop Loss", below_stop)
    
    st.markdown("---")
    
    # Aggregate local portfolio holdings for the watchlisted symbols
    holdings = db.query(
        PortfolioHolding.symbol,
        func.sum(PortfolioHolding.quantity).label("quantity"),
        func.sum(PortfolioHolding.quantity * PortfolioHolding.purchase_price).label("purchase_value"),
        func.max(PortfolioHolding.current_price).label("current_price"),
    ).filter(
        PortfolioHolding.symbol.in_([item.symbol for item in watchlist_items]),
        ~PortfolioHolding.account_type.in_(EXCLUDED_ACCOUNT_TYPES),
    ).group_by(PortfolioHolding.symbol).all()
    holdings_by_symbol = {h.symbol: h for h in holdings}

    # Watchlist table
    st.subheader("Your Watchlist")

    data = []
    watchlist_updated = False
    for item in watchlist_items:
        latest, prev = prices.get(item.symbol, (None, None))
        holding = holdings_by_symbol.get(item.symbol)

        # Prefer local holding current price, fall back to latest market price
        if holding and holding.current_price is not None:
            current_price = holding.current_price
        elif latest:
            current_price = latest.close_price
        else:
            current_price = None

        # Use holding purchase price (weighted avg) if available, else the stored watchlist value
        if holding and holding.quantity:
            purchase_price = holding.purchase_value / holding.quantity
            if item.purchase_price is None or abs(item.purchase_price - purchase_price) > 1e-6:
                item.purchase_price = purchase_price
                watchlist_updated = True
        else:
            purchase_price = item.purchase_price

        if current_price is not None:
            if purchase_price:
                gain_loss = current_price - purchase_price
                gain_loss_pct = (gain_loss / purchase_price) * 100
            else:
                gain_loss = 0
                gain_loss_pct = 0

            # Daily change vs previous day's close
            daily_change = (
                ((current_price - prev.close_price) / prev.close_price) * 100
                if prev and prev.close_price else 0.0
            )

            data.append({
                "Symbol": format_stock_label(item.symbol, item.stock.name if item.stock else None),
                "Current Price": float(current_price) if current_price is not None else float("nan"),
                "Daily Change": float(daily_change) if current_price is not None else 0.0,
                "Purchase Price": float(purchase_price) if purchase_price is not None else float("nan"),
                "Target Price": float(item.target_price) if item.target_price is not None else float("nan"),
                "Stop Loss": float(item.stop_loss) if item.stop_loss is not None else float("nan"),
                "Gain/Loss": float(gain_loss) if purchase_price is not None else float("nan"),
                "Gain/Loss %": float(gain_loss_pct) if purchase_price is not None else float("nan"),
                "_gain_loss": gain_loss if purchase_price is not None else None,
            })

    if watchlist_updated:
        db.commit()
    
    if data:
        watchlist_table = pd.DataFrame(data)
        styled_table = watchlist_table.style.apply(style_gain_loss_row, axis=1).hide(
            axis="columns", subset=["_gain_loss"]
        )
        st.dataframe(
            styled_table,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Symbol": st.column_config.TextColumn("Symbol", help="Ticker symbol and company name."),
                "Current Price": st.column_config.NumberColumn("Current Price", help="Latest market price per share.", format="R %.2f"),
                "Daily Change": st.column_config.NumberColumn("Daily Change", help="Change since the previous close.", format="%.2f%%"),
                "Purchase Price": st.column_config.NumberColumn("Purchase Price", help="Average purchase price tracked for this stock.", format="R %.2f"),
                "Target Price": st.column_config.NumberColumn("Target Price", help="Target alert price.", format="R %.2f"),
                "Stop Loss": st.column_config.NumberColumn("Stop Loss", help="Stop-loss alert price.", format="R %.2f"),
                "Gain/Loss": st.column_config.NumberColumn("Gain/Loss", help="Current price vs purchase price.", format="R %.2f"),
                "Gain/Loss %": st.column_config.NumberColumn("Gain/Loss %", help="Percentage gain or loss.", format="%.2f%%"),
            },
        )
    
    st.markdown("---")

    # Edit target / stop loss
    with st.expander("✏️ Edit Target / Stop Loss"):
        watchlist_names = {item.symbol: item.stock.name if item.stock else None for item in watchlist_items}
        symbol_to_edit = st.selectbox(
            "Select stock",
            [item.symbol for item in watchlist_items],
            format_func=lambda s: format_stock_label(s, watchlist_names.get(s)),
            key="edit_watchlist_symbol",
        )
        item_to_edit = next((i for i in watchlist_items if i.symbol == symbol_to_edit), None)

        col1, col2 = st.columns(2)
        with col1:
            new_target = st.number_input(
                "Target Price",
                min_value=0.0,
                step=0.01,
                value=item_to_edit.target_price or 0.0 if item_to_edit else 0.0,
                key=f"edit_target_{symbol_to_edit}",
            )
        with col2:
            new_stop = st.number_input(
                "Stop Loss",
                min_value=0.0,
                step=0.01,
                value=item_to_edit.stop_loss or 0.0 if item_to_edit else 0.0,
                key=f"edit_stop_{symbol_to_edit}",
            )

        if st.button("Update") and item_to_edit:
            _edit_watchlist_item(db, item_to_edit.symbol, new_target, new_stop)
            st.success(f"Updated {item_to_edit.symbol} target/stop loss.")
            st.rerun()

    st.markdown("---")
    
    # Remove from watchlist
    st.subheader("Remove from Watchlist")
    symbols_to_remove = st.multiselect(
        "Select stocks to remove",
        [item.symbol for item in watchlist_items],
        format_func=lambda s: format_stock_label(s, watchlist_names.get(s))
    )
    
    if st.button("Remove Selected") and symbols_to_remove:
        _remove_from_watchlist(db, symbols_to_remove)
        st.success(f"Removed {len(symbols_to_remove)} stocks from watchlist.")
        st.rerun()


def _add_to_watchlist(db: Session, symbol: str, target_price: float, purchase_price: float, stop_loss: float, notes: str):
    """Add a stock to the watchlist."""
    # Check if stock exists in database
    stock = db.query(Stock).filter(Stock.symbol == symbol).first()
    
    if not stock:
        # Create stock entry
        from app.services.data_service import get_company_name
        stock = Stock(symbol=symbol, name=get_company_name(symbol))
        db.add(stock)
        db.flush()
    
    # Check if already in watchlist
    existing = db.query(WatchlistItem).filter(
        WatchlistItem.symbol == symbol,
        WatchlistItem.is_active == True
    ).first()
    
    if existing:
        logger.warning(f"{symbol} already in watchlist")
        return
    
    # Add to watchlist
    item = WatchlistItem(
        stock_id=stock.id,
        symbol=symbol,
        target_price=target_price if target_price > 0 else None,
        purchase_price=purchase_price if purchase_price > 0 else None,
        stop_loss=stop_loss if stop_loss > 0 else None,
        notes=notes if notes else None
    )
    
    db.add(item)
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


def _remove_from_watchlist(db: Session, symbols: list):
    """Remove stocks from watchlist."""
    db.query(WatchlistItem).filter(
        WatchlistItem.symbol.in_(symbols)
    ).update({"is_active": False})
    db.commit()


def _edit_watchlist_item(db: Session, symbol: str, target_price: float, stop_loss: float):
    """Update a watchlist item's target price and stop loss."""
    item = db.query(WatchlistItem).filter(
        WatchlistItem.symbol == symbol,
        WatchlistItem.is_active == True,
    ).first()

    if not item:
        logger.warning(f"Cannot edit {symbol}: not in watchlist")
        return

    item.target_price = target_price if target_price > 0 else None
    item.stop_loss = stop_loss if stop_loss > 0 else None
    db.commit()



