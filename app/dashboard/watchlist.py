import streamlit as st
from sqlalchemy.orm import Session
from app.models.stock import Stock, StockPrice
from app.models.watchlist import WatchlistItem
from app.config.settings import settings
from app.dashboard.utils import format_stock_label
from datetime import datetime, timedelta
from loguru import logger


def show_watchlist(db: Session):
    """Display the watchlist page."""
    st.header("📋 Watchlist")
    st.markdown("Manage your stock watchlist and track performance.")
    
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
    
    # Display watchlist
    watchlist_items = db.query(WatchlistItem).filter(
        WatchlistItem.is_active == True
    ).all()
    
    if not watchlist_items:
        st.info("No stocks in watchlist. Add stocks above to start tracking.")
        return
    
    # Summary metrics
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.metric("Total Stocks", len(watchlist_items))
    
    with col2:
        above_target = sum(1 for item in watchlist_items if item.target_price and _get_current_price(db, item.symbol) >= item.target_price)
        st.metric("Above Target", above_target)
    
    with col3:
        below_stop = sum(1 for item in watchlist_items if item.stop_loss and _get_current_price(db, item.symbol) <= item.stop_loss)
        st.metric("Below Stop Loss", below_stop)
    
    st.markdown("---")
    
    # Watchlist table
    st.subheader("Your Watchlist")
    
    data = []
    for item in watchlist_items:
        current_price = _get_current_price(db, item.symbol)
        
        if current_price:
            if item.purchase_price:
                gain_loss = current_price - item.purchase_price
                gain_loss_pct = (gain_loss / item.purchase_price) * 100
            else:
                gain_loss = 0
                gain_loss_pct = 0
            
            # Get daily change
            daily_change = _get_daily_change(db, item.symbol)
            
            data.append({
                "Symbol": format_stock_label(item.symbol, item.stock.name if item.stock else None),
                "Current Price": f"R {current_price:.2f}",
                "Daily Change": f"{daily_change:+.2f}%",
                "Purchase Price": f"R {item.purchase_price:.2f}" if item.purchase_price else "N/A",
                "Target Price": f"R {item.target_price:.2f}" if item.target_price else "N/A",
                "Stop Loss": f"R {item.stop_loss:.2f}" if item.stop_loss else "N/A",
                "Gain/Loss": f"R {gain_loss:.2f}",
                "Gain/Loss %": f"{gain_loss_pct:+.2f}%",
            })
    
    if data:
        st.dataframe(data, use_container_width=True)
    
    st.markdown("---")
    
    # Remove from watchlist
    st.subheader("Remove from Watchlist")
    watchlist_names = {item.symbol: item.stock.name if item.stock else None for item in watchlist_items}
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


def _get_current_price(db: Session, symbol: str) -> float:
    """Get current price for a symbol."""
    price = db.query(StockPrice).filter(
        StockPrice.symbol == symbol
    ).order_by(StockPrice.timestamp.desc()).first()
    
    return price.close_price if price else 0.0


def _get_daily_change(db: Session, symbol: str) -> float:
    """Calculate daily percentage change."""
    latest = db.query(StockPrice).filter(
        StockPrice.symbol == symbol
    ).order_by(StockPrice.timestamp.desc()).first()
    
    if not latest:
        return 0.0
    
    prev = db.query(StockPrice).filter(
        StockPrice.symbol == symbol,
        StockPrice.timestamp < latest.timestamp
    ).order_by(StockPrice.timestamp.desc()).first()
    
    if prev and prev.close_price > 0:
        return ((latest.close_price - prev.close_price) / prev.close_price) * 100
    
    return 0.0
