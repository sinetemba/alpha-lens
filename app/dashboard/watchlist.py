import pandas as pd
import streamlit as st
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from app.models.stock import Stock, StockPrice
from app.models.watchlist import WatchlistItem
from sqlalchemy.exc import IntegrityError
from app.models.portfolio import PortfolioHolding
from app.dashboard.utils import (
    format_stock_label,
    style_price_columns,
    render_csv_download,
    render_market_data_refresh_control,
    render_symbol_search,
    style_gain_loss_row,
)
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

    # Post-action feedback must render at page top: popped inside the
    # collapsed add-expander it's never seen.
    flash = st.session_state.pop("watchlist_add_flash", None)
    if flash:
        level, message = flash
        getattr(st, level)(message)

    # Add new stock to watchlist
    with st.expander("➕ Add Stock to Watchlist"):
        col1, col2 = st.columns(2)
        
        with col1:
            symbol = render_symbol_search(db, key="watchlist_add")
        
        with col2:
            target_price = st.number_input("Target Price", min_value=0.0, step=0.01)
        
        purchase_price = st.number_input("Purchase Price", min_value=0.0, step=0.01)
        stop_loss = st.number_input("Stop Loss", min_value=0.0, step=0.01)
        alert_move = st.checkbox("Alert me on a daily move beyond a threshold")
        move_threshold = st.number_input(
            "Move threshold %", min_value=0.5, max_value=50.0, value=5.0, step=0.5
        ) if alert_move else 5.0
        notes = st.text_area("Notes")

        if st.button("Add to Watchlist"):
            if symbol:
                saved = _add_to_watchlist(
                    db, symbol, target_price, purchase_price, stop_loss, notes,
                    alert_move, move_threshold,
                )
                if saved is None:
                    st.warning(f"{symbol} is already on your watchlist.")
                else:
                    # Show the result via a post-rerun flash: st.rerun() wipes
                    # messages rendered in this run, so a same-run st.success
                    # would never be seen.
                    if saved == 0:
                        st.session_state["watchlist_add_flash"] = (
                            "warning",
                            f"Added {symbol}, but no price history could be fetched - "
                            "double-check that the ticker is correct.",
                        )
                    else:
                        st.session_state["watchlist_add_flash"] = (
                            "success", f"Added {symbol} to watchlist!"
                        )
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
    prices = _backfill_missing_prices(db, watchlist_items, prices)

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
        # JSE-priced accounts only: a bare US ticker (USD account) can share a
        # symbol with a JSE listing and would leak foreign prices/quantities
        # into the Rand columns here.
        ~func.upper(PortfolioHolding.account_type).like("%USD%"),
    ).group_by(PortfolioHolding.symbol).all()
    holdings_by_symbol = {h.symbol: h for h in holdings}

    # Watchlist table
    st.subheader("Your Watchlist")
    filter_choice = st.radio(
        "Filter",
        ["All", "Above Target", "Gain", "Loss"],
        index=0,
        horizontal=True,
        key="watchlist_filter",
    )

    data = []
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
        else:
            purchase_price = item.purchase_price

        if current_price is not None and purchase_price:
            gain_loss = current_price - purchase_price
            gain_loss_pct = (gain_loss / purchase_price) * 100
        else:
            gain_loss = None
            gain_loss_pct = None

        daily_change = (
            latest.close_price - prev.close_price
            if latest and latest.close_price and prev and prev.close_price else None
        )
        daily_change_pct = (
            (daily_change / prev.close_price) * 100
            if daily_change is not None else None
        )

        # Include the row even with no price data - silently dropping it makes
        # the item count disagree with the table and hides unresolvable symbols.
        # Keep these numeric (not pre-formatted strings) so the interactive
        # table sorts them by value, not lexicographically - NumberColumn
        # below handles the "R "/"%" display formatting.
        data.append({
            "Symbol": format_stock_label(item.symbol, item.stock.name if item.stock else None),
            "Current Price": current_price,
            "Change": daily_change,
            "Change %": daily_change_pct,
            "Purchase Price": purchase_price if purchase_price else None,
            "Target Price": item.target_price,
            # Distance to the target as a signed %: >= 0 means at/above
            # target (the old _above_target flag), negative shows how much
            # room is left - sortable, unlike the boolean it replaced.
            "To Target %": (
                ((current_price - item.target_price) / item.target_price) * 100
                if item.target_price and current_price is not None else None
            ),
            "Stop Loss": item.stop_loss,
            "Gain/Loss": gain_loss,
            "Gain/Loss %": gain_loss_pct,
        })

    # Filter categories match the row styling: "Above Target" rows render
    # darkgreen, gain rows green, loss rows red (None/zero = no data/break-even,
    # only visible under "All").
    above_target = lambda r: r["To Target %"] is not None and r["To Target %"] >= 0
    if filter_choice == "Above Target":
        data = [r for r in data if above_target(r)]
    elif filter_choice == "Gain":
        data = [r for r in data if not above_target(r) and (r["Gain/Loss"] or 0) > 0]
    elif filter_choice == "Loss":
        data = [r for r in data if (r["Gain/Loss"] or 0) < 0]

    if data:
        watchlist_table = pd.DataFrame(data)
        styled_table = watchlist_table.style.apply(_style_watchlist_row, axis=1).hide(
            axis="columns", subset=[c for c in watchlist_table.columns if c.startswith("_")]
        )
        # Per-row decimals: a penny stock (SKA at R0.0085) must not put every
        # row on 4 decimals (R 210.3800) - column formats are table-wide.
        styled_table = style_price_columns(
            styled_table, watchlist_table, "Current Price",
            price_cols=("Current Price", "Purchase Price", "Target Price", "Stop Loss"),
            signed_cols=("Change", "Gain/Loss"),
        )
        st.dataframe(
            styled_table,
            width="stretch",
            hide_index=True,
            column_config={
                "Symbol": st.column_config.TextColumn("Symbol", help="Ticker symbol and company name."),
                "Current Price": st.column_config.NumberColumn("Current Price", help="Latest market price per share."),
                "Change": st.column_config.NumberColumn("Change", help="Rand change since the previous close."),
                "Change %": st.column_config.NumberColumn("Change %", format="%+.2f%%", help="Percentage change since the previous close."),
                "Purchase Price": st.column_config.NumberColumn("Purchase Price", help="Average price paid per share, if held."),
                "Target Price": st.column_config.NumberColumn("Target Price", help="Price target for this stock."),
                "To Target %": st.column_config.NumberColumn("To Target %", format="%+.2f%%", help="Distance from target price - >=0 means the target has been hit or exceeded."),
                "Stop Loss": st.column_config.NumberColumn("Stop Loss", help="Stop loss price for this stock."),
                "Gain/Loss": st.column_config.NumberColumn("Gain/Loss", help="Current price less purchase price."),
                "Gain/Loss %": st.column_config.NumberColumn("Gain/Loss %", format="%+.2f%%", help="Gain or loss as a percentage of purchase price."),
            },
        )
        st.markdown(
            "**Legend:** "
            '<span style="background-color: darkgreen; color: white; padding: 2px 8px; border-radius: 4px;">At/above target</span> '
            '<span style="background-color: #dcfce7; color: #1f2937; padding: 2px 8px; border-radius: 4px;">Gain</span> '
            '<span style="background-color: #fee2e2; color: #1f2937; padding: 2px 8px; border-radius: 4px;">Loss</span> '
            '<span style="background-color: #fef3c7; color: #1f2937; padding: 2px 8px; border-radius: 4px;">No data / break-even</span>',
            unsafe_allow_html=True,
        )
        render_csv_download(watchlist_table, "watchlist", key="watchlist_csv")
    else:
        st.info(f"No stocks match the '{filter_choice}' filter.")

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

        alert_move_edit = st.checkbox(
            "Alert me on a daily move beyond a threshold",
            value=bool(
                item_to_edit
                and (item_to_edit.notify_price_increase or item_to_edit.notify_price_decrease)
            ),
            key=f"edit_alert_{symbol_to_edit}",
        )
        move_threshold_edit = st.number_input(
            "Move threshold %",
            min_value=0.5,
            max_value=50.0,
            value=(item_to_edit.price_change_threshold or 5.0) if item_to_edit else 5.0,
            step=0.5,
            key=f"edit_threshold_{symbol_to_edit}",
        ) if alert_move_edit else 5.0
        new_notes = st.text_area(
            "Notes",
            value=item_to_edit.notes or "" if item_to_edit else "",
            key=f"edit_notes_{symbol_to_edit}",
        )

        if st.button("Update") and item_to_edit:
            _edit_watchlist_item(
                db, item_to_edit.symbol, new_target, new_stop,
                alert_move_edit, move_threshold_edit, new_notes,
            )
            st.session_state["watchlist_add_flash"] = (
                "success", f"Updated {item_to_edit.symbol}."
            )
            st.rerun()

    st.markdown("---")
    
    # Remove from watchlist
    st.subheader("Remove from Watchlist")
    symbols_to_remove = st.multiselect(
        "Select stocks to remove",
        [item.symbol for item in watchlist_items],
        format_func=lambda s: format_stock_label(s, watchlist_names.get(s))
    )

    remove_key = "watchlist_remove_pending"
    if st.button("Remove Selected") and symbols_to_remove:
        st.session_state[remove_key] = tuple(symbols_to_remove)

    pending = st.session_state.get(remove_key)
    if pending:
        st.warning(f"Remove {len(pending)} stock{'s' if len(pending) != 1 else ''} ({', '.join(pending)}) from the watchlist?")
        confirm_col, cancel_col = st.columns(2)
        with confirm_col:
            if st.button("Yes, remove", key="watchlist_confirm_remove"):
                _remove_from_watchlist(db, list(pending))
                st.session_state[remove_key] = None
                st.session_state["watchlist_add_flash"] = (
                    "success", f"Removed {len(pending)} stocks from watchlist."
                )
                st.rerun()
        with cancel_col:
            if st.button("Cancel", key="watchlist_cancel_remove"):
                st.session_state[remove_key] = None
                st.rerun()


def _add_to_watchlist(
    db: Session,
    symbol: str,
    target_price: float,
    purchase_price: float,
    stop_loss: float,
    notes: str,
    notify_move: bool = False,
    move_threshold: float = 5.0,
) -> int | None:
    """Add a stock to the watchlist.

    Returns the number of price points backfilled, or None when the symbol is
    already on the watchlist (nothing was added).
    """
    # Check if already in watchlist
    existing = db.query(WatchlistItem).filter(
        WatchlistItem.symbol == symbol,
        WatchlistItem.is_active == True
    ).first()

    if existing:
        logger.warning(f"{symbol} already in watchlist")
        return None

    # Check if stock exists in database
    stock = db.query(Stock).filter(Stock.symbol == symbol).first()

    if not stock:
        # Create stock entry
        from app.services.data_service import get_company_name
        try:
            stock = Stock(symbol=symbol, name=get_company_name(symbol))
            db.add(stock)
            db.flush()
        except IntegrityError:
            # The updater's _ensure_stock can insert the same symbol between
            # our lookup and flush - take their row instead of crashing.
            db.rollback()
            stock = db.query(Stock).filter(Stock.symbol == symbol).first()
            if not stock:
                raise

    # Add to watchlist
    item = WatchlistItem(
        stock_id=stock.id,
        symbol=symbol,
        target_price=target_price if target_price > 0 else None,
        purchase_price=purchase_price if purchase_price > 0 else None,
        stop_loss=stop_loss if stop_loss > 0 else None,
        notes=notes if notes else None,
        notify_price_increase=notify_move,
        notify_price_decrease=notify_move,
        price_change_threshold=move_threshold,
    )

    db.add(item)
    try:
        db.commit()
    except IntegrityError:
        # Lost the race against a concurrent add of the same symbol (the
        # partial unique index on active items rejects the second insert).
        db.rollback()
        logger.info(f"{symbol} was added to the watchlist concurrently")
        return None

    # Backfill historical prices and technical indicators for the newly added stock
    saved = 0
    try:
        from app.services.data_service import DataService
        with st.spinner(f"Fetching price history for {symbol}..."):
            data_service = DataService(db)
            saved = data_service.update_historical_data(symbol)
        logger.info(f"Fetched {saved} price points for {symbol}")
    except Exception as e:
        logger.error(f"Failed to fetch price data for {symbol}: {e}")

    # A symbol that produced no history and has no stored prices is almost
    # certainly a bad ticker - deactivate it so it doesn't pollute the
    # Company View selector or get tracked by the updater. A provider outage
    # on an already-priced stock never triggers deactivation, and re-adding
    # a previously-failed ticker reactivates it.
    has_prices = db.query(StockPrice).filter(StockPrice.symbol == symbol).first() is not None
    if saved > 0 and not stock.is_active:
        stock.is_active = True
        db.commit()
    elif saved == 0 and not has_prices:
        stock.is_active = False
        db.commit()

    # The Company View selector is cached for 5 min - drop it so a
    # just-added (or reactivated) stock shows up immediately.
    from app.dashboard.company import _get_active_stocks
    _get_active_stocks.clear()
    return saved


def _remove_from_watchlist(db: Session, symbols: list):
    """Remove stocks from watchlist."""
    db.query(WatchlistItem).filter(
        WatchlistItem.symbol.in_(symbols)
    ).update({"is_active": False})
    db.commit()


def _edit_watchlist_item(
    db: Session,
    symbol: str,
    target_price: float,
    stop_loss: float,
    notify_move: bool = False,
    move_threshold: float = 5.0,
    notes: str = "",
):
    """Update a watchlist item's target/stop-loss, alert preferences and notes."""
    item = db.query(WatchlistItem).filter(
        WatchlistItem.symbol == symbol,
        WatchlistItem.is_active == True,
    ).first()

    if not item:
        logger.warning(f"Cannot edit {symbol}: not in watchlist")
        return

    item.target_price = target_price if target_price > 0 else None
    item.stop_loss = stop_loss if stop_loss > 0 else None
    item.notify_price_increase = notify_move
    item.notify_price_decrease = notify_move
    # Only overwrite the threshold when alerts are on - editing with the
    # checkbox off must not silently reset a stored custom threshold to 5.0.
    if notify_move:
        item.price_change_threshold = move_threshold
    item.notes = notes or None
    db.commit()


def _backfill_missing_prices(db: Session, watchlist_items: list, prices: dict) -> dict:
    """Fetch history for watchlist symbols that have no price data.

    Runs at most once per session per symbol (tracked in session state) so a
    bad ticker doesn't re-trigger network calls on every page rerun. Returns
    a refreshed price map when anything was fetched.
    """
    missing = [
        item.symbol for item in watchlist_items
        if (latest := prices.get(item.symbol, (None, None))[0]) is None
        or latest.close_price is None
    ]
    if not missing:
        return prices

    attempted = st.session_state.get("watchlist_backfill_attempted") or set()
    to_fetch = [s for s in missing if s not in attempted]
    if not to_fetch:
        return prices

    attempted.update(to_fetch)
    st.session_state["watchlist_backfill_attempted"] = attempted

    from app.services.data_service import DataService
    data_service = DataService(db)
    fetched_any = False
    for symbol in to_fetch:
        try:
            with st.spinner(f"Fetching price history for {symbol}..."):
                saved = data_service.update_historical_data(symbol)
            if saved:
                fetched_any = True
                st.success(f"Fetched {saved} price points for {symbol}.")
            else:
                st.warning(f"No price data found for {symbol} - check that the ticker is correct.")
        except Exception as e:
            logger.error(f"Failed to backfill price data for {symbol}: {e}")
            st.warning(f"Failed to fetch price data for {symbol}.")

    if not fetched_any:
        return prices

    # The price map is cached on (session, symbols), so clear it before
    # re-querying or the just-saved bars won't show up.
    _get_latest_and_previous_prices.clear()
    return _get_latest_and_previous_prices(db, [item.symbol for item in watchlist_items])


def _style_watchlist_row(row: pd.Series) -> list[str]:
    """Colour a watchlist row darkgreen when the current price is at/above target."""
    to_target = row.get("To Target %")
    if to_target is not None and to_target >= 0:
        return ["background-color: darkgreen; color: white"] * len(row)
    return style_gain_loss_row(row)



