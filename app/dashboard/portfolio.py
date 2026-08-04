from collections import Counter
import pandas as pd
import streamlit as st
from sqlalchemy.orm import Session
from app.models.portfolio import Portfolio, PortfolioHolding
from app.models.stock import Stock, StockPrice
from app.dashboard.utils import format_stock_label, style_gain_loss_row, is_market_data_stale, _to_display_tz
from app.collectors.easyequities import EasyEquitiesCollector
from datetime import datetime, timedelta, timezone
from loguru import logger


EXCLUDED_PORTFOLIO_SYMBOLS = {"SZK"}
EXCLUDED_ACCOUNT_TYPES = {"Demo ZAR"}


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
        ~PortfolioHolding.account_type.in_(EXCLUDED_ACCOUNT_TYPES),
    ).order_by(PortfolioHolding.purchase_date.asc()).all()
    refresh_requested, sync_caption = _render_easyequities_refresh_control(collector, portfolio)

    # Track EasyEquities sync recency via a persistent DB column (rather than
    # holdings.updated_at, which is also bumped by unrelated writes like
    # manual "Add Holding" or the local price fallback, and rather than
    # session_state, which resets on every app restart / new browser session).
    should_sync = collector.enabled and (
        refresh_requested or not holdings or is_market_data_stale(portfolio.last_easyequities_sync)
    )

    synced_holdings = None
    if collector.enabled:
        if should_sync:
            spinner_message = "Refreshing portfolio from EasyEquities..." if refresh_requested else "Syncing portfolio from EasyEquities..."
            with st.spinner(spinner_message):
                synced_holdings = _sync_portfolio_from_easyequities(db, portfolio.id, collector)
            if synced_holdings is None:
                error_detail = f" Error: {collector.last_error}" if collector.last_error else ""
                st.warning(f"EasyEquities sync failed. Showing the most recent database values.{error_detail}")
            else:
                portfolio.last_easyequities_sync = datetime.now(timezone.utc)
                db.commit()
                if refresh_requested:
                    st.success(f"Synced {synced_holdings} holding{'s' if synced_holdings != 1 else ''} from EasyEquities.")
        else:
            # Data is still fresh (synced within cache_ttl_seconds) — skip the network
            # round-trip to EasyEquities on this rerun and keep the last synced values.
            synced_holdings = 0
    else:
        st.info("Set EASYEQUITIES_USERNAME and EASYEQUITIES_PASSWORD in .env to enable live portfolio sync.")

    holdings = db.query(PortfolioHolding).filter(
        PortfolioHolding.portfolio_id == portfolio.id,
        ~PortfolioHolding.symbol.in_(EXCLUDED_PORTFOLIO_SYMBOLS),
        ~PortfolioHolding.account_type.in_(EXCLUDED_ACCOUNT_TYPES),
    ).order_by(PortfolioHolding.purchase_date.asc()).all()
    _update_easyequities_sync_caption(sync_caption, portfolio)
    if synced_holdings is None:
        for holding in holdings:
            _update_holding_values(db, holding)
    else:
        # EasyEquities sync only touches symbols it actually reports. Manually
        # added holdings (not held on EasyEquities) never get current_price
        # set otherwise, so fill those gaps from local price data without
        # touching holdings EasyEquities already priced.
        for holding in holdings:
            if holding.current_price is None:
                _update_holding_values(db, holding)
    _update_portfolio_totals(db, portfolio, holdings)

    # Pull Funds to Invest per EasyEquities account
    account_funds: dict[str, float] = {}
    if collector.enabled:
        account_funds = collector.get_funds_to_invest_by_account() or {}
        st.session_state["ee_last_funds_raw"] = getattr(collector, "_last_funds_raw", {})

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
    _render_instrument_type_counts(st.session_state.get("ee_last_raw_holdings"))
    
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

        if not collector.enabled:
            st.info("Set EASYEQUITIES_USERNAME and EASYEQUITIES_PASSWORD in .env to enable this.")
        else:
            if st.button("Fetch EasyEquities Accounts"):
                with st.spinner("Logging in to EasyEquities..."):
                    accounts = collector.list_accounts()

                if accounts:
                    st.session_state["ee_accounts"] = accounts
                else:
                    error_msg = "Could not fetch accounts. Check your EasyEquities credentials."
                    if collector.last_error:
                        error_msg += f"\n\nError: {collector.last_error}"
                    st.error(error_msg)
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

    # Holdings tables — one per account type
    if not holdings:
        st.info("No holdings in portfolio. Add holdings above.")
        return

    # Filters
    st.subheader("Filter Holdings")
    f_col1, f_col2, f_col3 = st.columns(3)
    with f_col1:
        search = st.text_input("Search by symbol or name", value="").strip().lower()
    with f_col2:
        account_options = sorted(set(h.account_type or "ZAR" for h in holdings))
        selected_accounts = st.multiselect("Account types", options=account_options, default=account_options)
    with f_col3:
        gain_choice = st.selectbox("Gain/Loss", options=["All", "Winning", "Losing"], index=0)

    gain_checks = {
        "All": lambda g: True,
        "Winning": lambda g: (g or 0) > 0,
        "Losing": lambda g: (g or 0) < 0,
    }

    def _matches(h: PortfolioHolding) -> bool:
        if selected_accounts and (h.account_type or "ZAR") not in selected_accounts:
            return False
        if search:
            name = h.stock.name if h.stock else ""
            if search not in h.symbol.lower() and (not name or search not in name.lower()):
                return False
        return gain_checks[gain_choice](h.gain_loss)

    display_holdings = [h for h in holdings if _matches(h)]

    if not display_holdings:
        st.info("No holdings match the selected filters.")
        return

    # Group holdings by account type
    account_groups: dict[str, list] = {}
    for h in display_holdings:
        account_groups.setdefault(h.account_type or "ZAR", []).append(h)

    # Render a table for each account type
    for account_type, group_holdings in account_groups.items():
        _render_holdings_table(db, account_type, group_holdings, account_funds.get(account_type))
    
    st.markdown("---")
    _render_easyequities_debug(collector, portfolio, holdings, account_funds)
    
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
    
    remove_key = "remove_pending_symbol"
    if st.button("Remove Holding"):
        st.session_state[remove_key] = symbol_to_remove

    if st.session_state.get(remove_key) == symbol_to_remove:
        st.warning(f"Are you sure you want to remove {symbol_to_remove}?")
        confirm_col, cancel_col = st.columns(2)
        with confirm_col:
            if st.button("Yes, remove"):
                _remove_holding(db, portfolio.id, symbol_to_remove)
                st.session_state[remove_key] = None
                st.success(f"Removed {symbol_to_remove} from portfolio.")
                st.rerun()
        with cancel_col:
            if st.button("Cancel"):
                st.session_state[remove_key] = None
                st.rerun()


def _get_group_latest_and_previous_prices(
    db: Session, symbols: list[str]
) -> dict[str, tuple[StockPrice | None, StockPrice | None]]:
    """Batch-fetch each symbol's latest price and its most recent prior-day price."""
    if not symbols:
        return {}

    rows = db.query(StockPrice).filter(
        StockPrice.symbol.in_(symbols)
    ).order_by(StockPrice.timestamp.desc()).all()

    by_symbol: dict[str, list[StockPrice]] = {}
    for row in rows:
        by_symbol.setdefault(row.symbol, []).append(row)

    result: dict[str, tuple[StockPrice | None, StockPrice | None]] = {}
    for symbol, prices in by_symbol.items():
        latest = prices[0]
        latest_day_start = latest.timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
        prev = next((p for p in prices if p.timestamp < latest_day_start), None)
        result[symbol] = (latest, prev)
    return result


def _render_holdings_table(
    db: Session, account_type: str, holdings: list, funds_to_invest: float | None = None
) -> None:
    """Render a single holdings table section for the given account type."""
    st.subheader(f"{account_type} Holdings")

    winning = sum(h.gain_loss is not None and h.gain_loss > 0 for h in holdings)
    losing = sum(h.gain_loss is not None and h.gain_loss < 0 for h in holdings)
    unchanged = len(holdings) - winning - losing
    total_value = sum(h.current_value or 0.0 for h in holdings)
    total_cost = sum(h.quantity * h.purchase_price for h in holdings)
    total_gl = total_value - total_cost
    total_gl_pct = (total_gl / total_cost * 100) if total_cost else 0.0

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Value", f"R {total_value:,.2f}")
    with col2:
        st.metric("Cost", f"R {total_cost:,.2f}")
    with col3:
        st.metric("Gain/Loss", f"R {total_gl:,.2f}")
    with col4:
        st.metric("Gain/Loss %", f"{total_gl_pct:+.2f}%")

    summary = f"\U0001f7e2 {winning} winning | \U0001f534 {losing} losing"
    if unchanged:
        summary += f" | \U0001f7e1 {unchanged} unchanged"
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

    if not data:
        st.info(f"No holdings in {account_type} account.")
        return

    if funds_to_invest is not None:
        st.metric("Funds to Invest", f"R {funds_to_invest:,.2f}")

    holdings_table = pd.DataFrame(data)
    available_columns = [c for c in holdings_table.columns if c != "_gain_loss"]
    table_key = f"portfolio_{account_type}_columns"
    if table_key not in st.session_state:
        st.session_state[table_key] = available_columns
    selected_columns = st.multiselect(
        "Columns to display",
        available_columns,
        key=table_key,
    )
    if selected_columns:
        visible_columns = [c for c in available_columns if c in selected_columns]
        visible_table = holdings_table[visible_columns + ["_gain_loss"]]
        styled_table = visible_table.style.apply(style_gain_loss_row, axis=1).hide(
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

    # Today's movement for this account group
    symbols = [h.symbol for h in holdings if h.symbol]
    prices = _get_group_latest_and_previous_prices(db, symbols)
    today_gain = 0.0
    yesterday_value = 0.0
    for h in holdings:
        latest, prev = prices.get(h.symbol, (None, None))
        if latest and prev and prev.close_price is not None and h.quantity:
            today_gain += (latest.close_price - prev.close_price) * h.quantity
            yesterday_value += prev.close_price * h.quantity

    today_pct = (today_gain / yesterday_value * 100) if yesterday_value else 0.0
    st.markdown(
        f"<small><strong>Today's Gain/Loss = R {today_gain:,.2f} | "
        f"Today's Gain/Loss % = {today_pct:+.2f}%</strong></small>",
        unsafe_allow_html=True,
    )

    st.markdown("---")


def _render_easyequities_refresh_control(
    collector: EasyEquitiesCollector, portfolio: Portfolio
) -> tuple[bool, object]:
    _, refresh_column = st.columns([3, 1])
    with refresh_column:
        refresh_requested = st.button(
            "🔄 Refresh Portfolio",
            key="portfolio_easyequities_refresh",
            disabled=not collector.enabled,
        )
        sync_caption = st.empty()
        _update_easyequities_sync_caption(sync_caption, portfolio)
    return refresh_requested, sync_caption


def _update_easyequities_sync_caption(sync_caption, portfolio: Portfolio) -> None:
    latest_update = portfolio.last_easyequities_sync
    if latest_update:
        display_update = _to_display_tz(latest_update)
        sync_caption.caption(f"Last EasyEquities sync: {display_update.strftime('%d %b %Y %H:%M')}")
    else:
        sync_caption.caption("No EasyEquities sync recorded yet.")


def _render_easyequities_debug(
    collector: EasyEquitiesCollector,
    portfolio: Portfolio,
    holdings: list,
    account_funds: dict[str, float] | None = None,
) -> None:
    """Show raw EasyEquities holdings and why each one is included/excluded."""
    if not collector.enabled:
        return

    with st.expander("Debug EasyEquities sync"):
        if collector.last_error:
            st.error(f"EasyEquities last error: {collector.last_error}")

        raw = st.session_state.get("ee_last_raw_holdings")
        if not raw:
            if st.button("Load raw EasyEquities data"):
                with st.spinner("Fetching raw data..."):
                    raw = collector.get_all_holdings()
                    st.session_state["ee_last_raw_holdings"] = raw or []
                st.rerun()
            return

        rows = []
        raw_total = 0.0
        filtered_total = 0.0
        filtered_count = 0
        for idx, h in enumerate(raw, start=1):
            current_value = h.get("current_value") or 0.0
            raw_total += current_value
            symbol = h.get("symbol", "")
            account = h.get("account_name", "ZAR")
            shares = h.get("shares", 0)
            reasons = []

            if account in EXCLUDED_ACCOUNT_TYPES:
                reasons.append(f"excluded account '{account}'")
            if symbol in EXCLUDED_PORTFOLIO_SYMBOLS:
                reasons.append(f"excluded symbol '{symbol}'")
            if shares <= 0:
                reasons.append("shares <= 0")

            if not reasons:
                filtered_total += current_value
                filtered_count += 1

            rows.append({
                "#": idx,
                "Symbol": symbol or "N/A",
                "Name": h.get("name", ""),
                "Account": account,
                "Shares": f"{shares:,.4f}" if shares else "0",
                "Current Value (raw)": f"R {current_value:,.2f}",
                "Included?": "No" if reasons else "Yes",
                "Reason if excluded": "; ".join(reasons),
            })

        st.dataframe(rows, use_container_width=True, hide_index=True)

        c1, c2, c3 = st.columns(3)
        c1.metric("Raw EasyEquities total", f"R {raw_total:,.2f}")
        c2.metric("Filtered total (included)", f"R {filtered_total:,.2f}")
        c3.metric("App portfolio value", f"R {portfolio.current_value:,.2f}")

        c4, c5, c6 = st.columns(3)
        c4.metric("Raw holdings count", len(raw))
        c5.metric("Filtered holdings count", filtered_count)
        c6.metric("App holdings count", len(holdings))

        if account_funds:
            st.subheader("Funds to Invest by Account")
            st.write(account_funds)

        raw_funds = st.session_state.get("ee_last_funds_raw")
        if raw_funds:
            st.subheader("Raw Funds Valuations")
            st.json(raw_funds)

        if st.button("Clear debug data"):
            st.session_state.pop("ee_last_raw_holdings", None)
            st.rerun()


def _render_instrument_type_counts(raw_holdings: list | None) -> None:
    """Show how many included holdings fall into each instrument type (Equity, ETF, etc.)."""
    if not raw_holdings:
        return

    type_counts = Counter()
    for h in raw_holdings:
        symbol = h.get("symbol", "")
        account = h.get("account_name", "ZAR")
        shares = h.get("shares", 0)

        if account in EXCLUDED_ACCOUNT_TYPES:
            continue
        if symbol in EXCLUDED_PORTFOLIO_SYMBOLS:
            continue
        if shares <= 0:
            continue

        type_counts[h.get("instrument_type", "Unknown")] += 1

    if not type_counts:
        return

    st.subheader("Holdings by Type")
    cols = st.columns(len(type_counts))
    for col, (instrument_type, count) in zip(cols, sorted(type_counts.items())):
        col.metric(instrument_type, count)


def _sync_portfolio_from_easyequities(
    db: Session, portfolio_id: int, collector: EasyEquitiesCollector
) -> int | None:
    ee_holdings = collector.get_all_holdings()
    st.session_state["ee_last_raw_holdings"] = ee_holdings if ee_holdings is not None else []
    if ee_holdings is None:
        return None

    aggregated_holdings = {}
    seen_account_types = set()
    for ee_holding in ee_holdings:
        symbol = ee_holding["symbol"]
        account_name = ee_holding.get("account_name", "ZAR")
        if account_name not in EXCLUDED_ACCOUNT_TYPES:
            seen_account_types.add(account_name)
        if (
            not symbol
            or symbol in EXCLUDED_PORTFOLIO_SYMBOLS
            or account_name in EXCLUDED_ACCOUNT_TYPES
            or ee_holding["shares"] <= 0
        ):
            continue
        key = (symbol, account_name)
        current = aggregated_holdings.setdefault(key, {
            "symbol": symbol,
            "name": ee_holding.get("name") or symbol,
            "account_name": account_name,
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

    # EasyEquities is the source of truth: remove holdings under the account
    # types we just synced that it no longer reports (fully sold/delisted
    # positions). Otherwise they linger in the DB forever and get retried on
    # every price refresh, which is slow for delisted/wrong-exchange tickers.
    if seen_account_types:
        stale_holdings = db.query(PortfolioHolding).filter(
            PortfolioHolding.portfolio_id == portfolio_id,
            PortfolioHolding.account_type.in_(seen_account_types),
        ).all()
        removed = 0
        for holding in stale_holdings:
            if (holding.symbol, holding.account_type) not in aggregated_holdings:
                db.delete(holding)
                removed += 1
        if removed:
            db.commit()
            logger.info(f"Removed {removed} stale holding(s) no longer reported by EasyEquities")

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
    account_type = ee_holding.get("account_name", "ZAR")
    if (
        not symbol
        or symbol in EXCLUDED_PORTFOLIO_SYMBOLS
        or account_type in EXCLUDED_ACCOUNT_TYPES
        or ee_holding["shares"] <= 0
    ):
        return

    stock = db.query(Stock).filter(Stock.symbol == symbol).first()
    if not stock:
        stock = Stock(symbol=symbol, name=ee_holding.get("name") or symbol)
        db.add(stock)
        db.flush()

    holding = db.query(PortfolioHolding).filter(
        PortfolioHolding.portfolio_id == portfolio_id,
        PortfolioHolding.symbol == symbol,
        PortfolioHolding.account_type == account_type,
    ).first()

    is_new = holding is None
    if is_new:
        holding = PortfolioHolding(
            portfolio_id=portfolio_id,
            stock_id=stock.id,
            symbol=symbol,
            account_type=account_type,
            purchase_date=datetime.now(timezone.utc),
        )
        db.add(holding)

    # EasyEquities is treated as the source of truth for quantity, cost basis, and market value on sync
    holding.quantity = ee_holding["shares"]
    holding.purchase_price = ee_holding["purchase_price"]
    holding.current_price = ee_holding["current_price"]
    holding.current_value = ee_holding["current_value"]
    holding.gain_loss = holding.current_value - (holding.quantity * holding.purchase_price)
    cost_basis = holding.quantity * holding.purchase_price
    holding.gain_loss_percentage = (
        (holding.gain_loss / cost_basis) * 100
        if cost_basis else 0.0
    )
    # Always update the timestamp so the UI reflects the latest sync time,
    # even when EasyEquities returns identical values.
    holding.updated_at = datetime.now(timezone.utc)

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
        cost_basis = holding.quantity * holding.purchase_price
        if cost_basis:
            holding.gain_loss = holding.current_value - cost_basis
            holding.gain_loss_percentage = (holding.gain_loss / cost_basis) * 100
        else:
            holding.gain_loss = holding.current_value
            holding.gain_loss_percentage = 0.0
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


