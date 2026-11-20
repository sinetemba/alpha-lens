from collections import Counter
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, case
from sqlalchemy.orm import Session, joinedload
from app.models.portfolio import (
    Portfolio,
    PortfolioHolding,
    EXCLUDED_ACCOUNT_TYPES,
    EXCLUDED_PORTFOLIO_SYMBOLS,
    is_jse_tracked_account,
)
from app.models.portfolio_snapshot import PortfolioSnapshot
from app.models.transaction import PortfolioTransaction
from app.models.stock import Stock, StockPrice
from app.dashboard.utils import (
    format_stock_label,
    is_market_data_stale,
    PriceSnapshot,
    style_price_columns,
    render_csv_download,
    render_last_fetch_caption,
    render_symbol_search,
    style_gain_loss_row,
    _to_display_tz,
)
from app.collectors.easyequities import EasyEquitiesCollector
from app.services.portfolio_valuation import update_holding_values, update_portfolio_totals
from app.services.price_queries import get_latest_and_previous_prices
# The sync itself is Streamlit-free in services.easyequities_sync so the
# headless updater can run it too; these names are re-exported for tests.
from app.services.easyequities_sync import (
    _normalize_account_type,
    _ee_reconcile_units,
    _sync_holding_from_easyequities,
    sync_portfolio_from_easyequities,
)
from datetime import datetime, timedelta, timezone
from loguru import logger


def show_portfolio(db: Session):
    """Display the portfolio page."""
    st.header("💼 Portfolio")
    st.markdown("Track your investment portfolio and performance.")
    
    # Get or create portfolio
    portfolio = db.query(Portfolio).first()
    
    if not portfolio:
        portfolio = Portfolio(name="My Portfolio")
        db.add(portfolio)
        try:
            db.commit()
            st.info("Created new portfolio. Add holdings below.")
        except IntegrityError:
            # A concurrent first-run load inserted the single portfolio row
            # first (uq_portfolios_singleton) - use theirs.
            db.rollback()
            portfolio = db.query(Portfolio).first()

    # Reuse the logged-in EasyEquities collector across Streamlit reruns.
    if "ee_collector" not in st.session_state:
        st.session_state["ee_collector"] = EasyEquitiesCollector()
    collector = st.session_state["ee_collector"]

    holdings = db.query(PortfolioHolding).options(
        joinedload(PortfolioHolding.stock)
    ).filter(
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
                portfolio.last_easyequities_sync = datetime.now(timezone.utc).replace(tzinfo=None)
                db.commit()
                if refresh_requested:
                    st.success(f"Synced {synced_holdings} holding{'s' if synced_holdings != 1 else ''} from EasyEquities.")
        else:
            # Data is still fresh (synced within cache_ttl_seconds) — skip the network
            # round-trip to EasyEquities on this rerun and keep the last synced values.
            synced_holdings = 0
    else:
        st.info("Set EASYEQUITIES_USERNAME and EASYEQUITIES_PASSWORD in .env to enable live portfolio sync.")

    holdings = db.query(PortfolioHolding).options(
        joinedload(PortfolioHolding.stock)
    ).filter(
        PortfolioHolding.portfolio_id == portfolio.id,
        ~PortfolioHolding.symbol.in_(EXCLUDED_PORTFOLIO_SYMBOLS),
        ~PortfolioHolding.account_type.in_(EXCLUDED_ACCOUNT_TYPES),
    ).order_by(PortfolioHolding.purchase_date.asc()).all()
    _update_easyequities_sync_caption(sync_caption, portfolio)
    if synced_holdings is None:
        _update_holding_values(db, holdings)
    else:
        # EasyEquities sync only touches symbols it actually reports. Manually
        # added holdings (not held on EasyEquities) never get current_price
        # set otherwise, so fill those gaps from local price data without
        # touching holdings EasyEquities already priced.
        _update_holding_values(db, [h for h in holdings if h.current_price is None])
    _update_portfolio_totals(db, portfolio, holdings)
    render_last_fetch_caption(db, [h.symbol for h in holdings])

    # Pull Funds to Invest per EasyEquities account. Refreshed only when a sync
    # actually ran (or nothing is cached yet) - not on every Streamlit rerun.
    account_funds: dict[str, float] = {}
    if collector.enabled:
        if (should_sync and synced_holdings is not None) or "ee_account_funds" not in st.session_state:
            account_funds = collector.get_funds_to_invest_by_account() or {}
            st.session_state["ee_account_funds"] = account_funds
            st.session_state["ee_last_funds_raw"] = getattr(collector, "_last_funds_raw", {})
        else:
            account_funds = st.session_state["ee_account_funds"]

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

    _render_unpriced_notice(db, holdings, portfolio)
    _render_value_history(db, portfolio.id)
    _render_sector_allocation(db, holdings)
    _render_transaction_history(db, portfolio.id)

    st.markdown("---")
    _render_instrument_type_counts(st.session_state.get("ee_last_raw_holdings"))
    
    st.markdown("---")
    
    # Add new holding
    with st.expander("➕ Add Holding"):
        col1, col2 = st.columns(2)
        
        with col1:
            symbol = render_symbol_search(db, key="portfolio_add")
            quantity = st.number_input("Quantity", min_value=0.0, step=1.0)
        
        with col2:
            purchase_price = st.number_input("Purchase Price", min_value=0.0, step=0.01)
            purchase_date = st.date_input(
                "Purchase Date", value=_to_display_tz(datetime.now(timezone.utc)).date()
            )
        
        flash = st.session_state.pop("portfolio_add_flash", None)
        if flash:
            level, message = flash
            getattr(st, level)(message)

        if st.button("Add Holding"):
            if symbol and quantity > 0 and purchase_price > 0:
                saved = _add_holding(db, portfolio.id, symbol, quantity, purchase_price, purchase_date)
                if saved is None:
                    st.warning(f"{symbol} is excluded from portfolio tracking - not added.")
                else:
                    # Flash the result post-rerun: st.rerun() discards messages
                    # rendered in this run.
                    if saved == 0:
                        st.session_state["portfolio_add_flash"] = (
                            "warning",
                            f"Added {symbol}, but no price history could be fetched - "
                            "double-check that the ticker is correct.",
                        )
                    else:
                        st.session_state["portfolio_add_flash"] = (
                            "success", f"Added {quantity} shares of {symbol} to portfolio!"
                        )
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
                        db.commit()
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
    
    # Remove holding — scoped to (symbol, account_type), since the same symbol
    # can legitimately be held in e.g. both the ZAR and TFSA accounts.
    st.subheader("Remove Holding")
    remove_flash = st.session_state.pop("portfolio_remove_flash", None)
    if remove_flash:
        getattr(st, remove_flash[0])(remove_flash[1])
    holding_options = {
        f"{format_stock_label(h.symbol, h.stock.name if h.stock else None)} — {h.account_type or 'ZAR'}": h
        for h in holdings
    }
    label_to_remove = st.selectbox("Select holding to remove", list(holding_options.keys()))
    holding_to_remove = holding_options.get(label_to_remove)
    if holding_to_remove is None:
        return

    reason = st.radio(
        "Reason",
        ["Sold it", "Entered by mistake"],
        horizontal=True,
        key="remove_holding_reason",
        help=(
            "'Sold it' records a sale (realized P&L) at the price/date below. "
            "'Entered by mistake' deletes the holding and its logged manual buys "
            "without recording a sale."
        ),
    )
    sold = reason == "Sold it"
    sell_price = sold_on = None
    if sold:
        price_col, date_col = st.columns(2)
        default_price = float(holding_to_remove.current_price or holding_to_remove.purchase_price or 0.0)
        sell_price = price_col.number_input(
            "Sale price per share (R)", min_value=0.0, value=default_price,
            step=0.01, format="%.4f", key="remove_holding_price",
        )
        sold_on = date_col.date_input("Sale date", value=datetime.now().date(), key="remove_holding_date")
    if holding_to_remove.account_type != "Manual":
        st.caption(
            "This holding comes from EasyEquities - if EasyEquities still reports it, "
            "the next sync will add it back."
        )

    remove_key = "remove_pending_holding"
    if st.button("Remove Holding"):
        st.session_state[remove_key] = label_to_remove

    if label_to_remove and st.session_state.get(remove_key) == label_to_remove:
        what = (
            f"record a sale of {label_to_remove} at R {sell_price:,.4f} and remove it"
            if sold else f"delete {label_to_remove} without recording a sale"
        )
        st.warning(f"Are you sure you want to {what}?")
        confirm_col, cancel_col = st.columns(2)
        with confirm_col:
            if st.button("Yes, remove"):
                _remove_holding(
                    db, portfolio.id,
                    holding_to_remove.symbol, holding_to_remove.account_type,
                    record_sale=sold,
                    sell_price=sell_price,
                    sold_at=datetime.combine(sold_on, datetime.min.time()) if sold_on else None,
                )
                st.session_state[remove_key] = None
                st.session_state["portfolio_remove_flash"] = (
                    "success", f"Removed {label_to_remove} from portfolio."
                )
                st.rerun()
        with cancel_col:
            if st.button("Cancel"):
                st.session_state[remove_key] = None
                st.rerun()


# A JSE holding with no stored bar newer than this is flagged as not live.
UNPRICED_AFTER_DAYS = 7


def _render_unpriced_notice(db: Session, holdings: list, portfolio: Portfolio) -> None:
    """Name JSE holdings whose value isn't live (suspended/delisted/unknown to the feeds).

    Their value stays whatever EasyEquities last reported, which is easy to
    mistake for a live price.
    """
    symbols = sorted({h.symbol for h in holdings if is_jse_tracked_account(h.account_type)})
    if not symbols:
        return
    latest = dict(
        db.query(StockPrice.symbol, func.max(StockPrice.timestamp))
        .filter(StockPrice.symbol.in_(symbols)).group_by(StockPrice.symbol).all()
    )
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=UNPRICED_AFTER_DAYS)
    stale = [
        f"{s} (last price {latest[s]:%Y-%m-%d})" if latest.get(s) else f"{s} (never priced)"
        for s in symbols if not latest.get(s) or latest[s] < cutoff
    ]
    if not stale:
        return
    synced = (
        _to_display_tz(portfolio.last_easyequities_sync).strftime("%Y-%m-%d %H:%M")
        if portfolio.last_easyequities_sync else "never"
    )
    st.caption(
        f"⚠️ No live JSE price for {', '.join(stale)} - likely suspended or delisted. "
        f"Their values are EasyEquities' last figures (synced {synced} SAST)."
    )


def _render_value_history(db: Session, portfolio_id: int) -> None:
    """Total value vs cost basis over time, from the daily snapshots."""
    rows = db.query(PortfolioSnapshot).filter(
        PortfolioSnapshot.portfolio_id == portfolio_id
    ).order_by(PortfolioSnapshot.snapshot_date).all()
    with st.expander("📈 Value over time", expanded=len(rows) >= 2):
        if len(rows) < 2:
            st.info(
                "Value history starts today - a snapshot is recorded each time prices "
                "update (3x daily by the updater, and whenever this page loads). "
                "The chart appears once there are two days of data."
            )
            return
        fig = go.Figure()
        dates = [r.snapshot_date for r in rows]
        if any(r.crypto_value or r.bullion_value for r in rows):
            # Net worth adds crypto and bullion (recorded by the updater);
            # days before they were tracked show JSE only.
            fig.add_trace(go.Scatter(
                x=dates,
                y=[r.total_value + (r.crypto_value or 0) + (r.bullion_value or 0) for r in rows],
                name="Net worth (JSE + crypto + bullion)", mode="lines+markers",
            ))
        fig.add_trace(go.Scatter(x=dates, y=[r.total_value for r in rows], name="JSE value", mode="lines+markers"))
        fig.add_trace(go.Scatter(
            x=dates, y=[r.total_cost for r in rows], name="JSE cost basis",
            mode="lines", line={"dash": "dash"},
        ))
        fig.update_layout(
            height=320, margin={"l": 10, "r": 10, "t": 10, "b": 10},
            yaxis_tickprefix="R ", yaxis_tickformat=",.0f", hovermode="x unified",
            legend={"orientation": "h", "y": 1.1},
        )
        st.plotly_chart(fig, width="stretch")
        first, last = rows[0], rows[-1]
        change = last.total_value - first.total_value
        st.caption(
            f"R {change:+,.2f} since {first.snapshot_date:%Y-%m-%d} "
            "(includes money added or withdrawn - this is value, not return)."
        )


def _render_sector_allocation(db: Session, holdings: list) -> None:
    """Invested value by sector (Stock.sector; 'Unclassified' when unfetched)."""
    sectors: dict[str, float] = {}
    for h in holdings:
        value = h.current_value or 0.0
        if value <= 0:
            continue
        sector = (h.stock.sector if h.stock else None) or "Unclassified"
        sectors[sector] = sectors.get(sector, 0.0) + value
    if not sectors:
        return
    with st.expander("🏷️ Sector allocation"):
        fig = go.Figure(go.Pie(
            labels=list(sectors), values=list(sectors.values()),
            hole=0.45, textinfo="label+percent",
        ))
        fig.update_layout(
            height=320, margin={"l": 10, "r": 10, "t": 10, "b": 10},
            showlegend=False,
        )
        st.plotly_chart(fig, width="stretch")
        if "Unclassified" in sectors:
            st.caption(
                "'Unclassified' = holdings whose sector hasn't been fetched yet "
                "(populated from Yahoo on the next price update)."
            )


def _render_transaction_history(db: Session, portfolio_id: int) -> None:
    """Buy/sell log + realized P&L (manual add/remove + EasyEquities sync events)."""
    realized, total_buys, sales = db.query(
        func.coalesce(func.sum(PortfolioTransaction.realized_gain), 0.0),
        func.coalesce(func.sum(
            case((PortfolioTransaction.transaction_type == "buy",
                  PortfolioTransaction.value), else_=0.0)
        ), 0.0),
        func.count(),
    ).filter(
        PortfolioTransaction.portfolio_id == portfolio_id,
    ).first()

    with st.expander("📜 Transaction history & realized P&L"):
        if not sales:
            st.info(
                "No transactions recorded yet. Buys and sells are logged when "
                "holdings are added/removed here or reconciled by EasyEquities sync."
            )
            return
        c1, c2, c3 = st.columns(3)
        c1.metric("Realized P&L (approx.)", f"R {realized:,.2f}")
        c2.metric("Total buys", f"R {total_buys:,.2f}")
        c3.metric("Transactions", sales)
        st.caption(
            "Approximate: gains use average cost, not per-lot. EasyEquities doesn't "
            "expose trade dates or fill prices, so its events are dated at sync time "
            "and priced from EasyEquities' last reported values. Moves between "
            "accounts are logged as xfer_in/xfer_out with no P&L. Holdings that "
            "predate the log have a 'backfill' opening buy."
        )

        rows = db.query(PortfolioTransaction).filter(
            PortfolioTransaction.portfolio_id == portfolio_id
        ).order_by(PortfolioTransaction.transacted_at.desc()).limit(200).all()
        table = pd.DataFrame([{
            "Date": _to_display_tz(r.transacted_at).strftime("%Y-%m-%d") if r.transacted_at else "",
            "Type": r.transaction_type,
            "Symbol": r.symbol,
            "Account": r.account_type or "ZAR",
            "Qty": r.quantity,
            "Price": r.price,
            "Value": r.value,
            "Realized": r.realized_gain,
            "Source": r.source,
        } for r in rows])
        # Price per row: a penny-stock trade (R0.0085) must not show as R0.01.
        st.dataframe(
            style_price_columns(table.style, table, "Price", price_cols=("Price",)),
            width="stretch", hide_index=True,
            column_config={
                "Qty": st.column_config.NumberColumn(format="%g"),
                "Price": st.column_config.NumberColumn(),
                "Value": st.column_config.NumberColumn(format="R %.2f"),
                "Realized": st.column_config.NumberColumn(format="R %.2f"),
            },
        )
        _render_transaction_delete(db, portfolio_id, rows)


def _render_transaction_delete(db: Session, portfolio_id: int, rows: list) -> None:
    """Two-step delete of one logged event (correcting a wrong entry)."""
    flash = st.session_state.pop("transaction_delete_flash", None)
    if flash:
        getattr(st, flash[0])(flash[1])
    options = {
        f"#{r.id} {r.transaction_type} {r.quantity:g} {r.symbol} ({r.account_type or 'ZAR'}) "
        f"@ R {r.price:,.4f}"
        f"{' on ' + _to_display_tz(r.transacted_at).strftime('%Y-%m-%d') if r.transacted_at else ''}": r.id
        for r in rows
    }
    col_select, col_button = st.columns([4, 1])
    label = col_select.selectbox(
        "Correct a wrong entry", list(options), key="transaction_delete_select",
    )
    pending_key = "transaction_delete_pending"
    if col_button.button("Delete", key="transaction_delete_button"):
        st.session_state[pending_key] = label
    if label and st.session_state.get(pending_key) == label:
        st.warning(f"Delete {label} from the history? This changes realized P&L.")
        yes_col, no_col = st.columns(2)
        if yes_col.button("Yes, delete", key="transaction_delete_yes"):
            from app.services.transactions import delete_transaction
            deleted = delete_transaction(db, portfolio_id, options[label])
            st.session_state[pending_key] = None
            st.session_state["transaction_delete_flash"] = (
                ("success", f"Deleted {label}.") if deleted
                else ("warning", "That transaction no longer exists.")
            )
            st.rerun()
        if no_col.button("Cancel", key="transaction_delete_cancel"):
            st.session_state[pending_key] = None
            st.rerun()


@st.cache_data(
    ttl=60,
    show_spinner=False,
    hash_funcs={
        Session: lambda db: id(db.bind),
        list: lambda x: hash(tuple(sorted(x))),
    },
)
def _get_group_latest_and_previous_prices(
    db: Session, symbols: list[str]
) -> dict[str, tuple[PriceSnapshot | None, PriceSnapshot | None]]:
    """Cached wrapper around the shared latest/previous-day price query."""
    return get_latest_and_previous_prices(db, symbols)


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
            # Keep these numeric (not pre-formatted strings) so the
            # interactive table sorts them by value, not lexicographically -
            # NumberColumn below handles the "R "/"%" display formatting.
            "Purchase Price": holding.purchase_price,
            "Current Price": holding.current_price,
            "Total Purchase Value": holding.quantity * holding.purchase_price,
            "Current Value": holding.current_value,
            "Gain/Loss": holding.gain_loss,
            "Gain/Loss %": holding.gain_loss_percentage,
            "Dividends": holding.total_dividends_received,
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
        # Per-row decimals keyed on the current price - taken from the full
        # table, since the user may have hidden the Current Price column.
        styled_table = style_price_columns(
            styled_table, visible_table, holdings_table["Current Price"],
            price_cols=("Purchase Price", "Current Price"),
        )
        st.dataframe(
            styled_table,
            width="stretch",
            hide_index=True,
            column_config={
                "Symbol": st.column_config.TextColumn("Symbol", help="Ticker symbol and company name."),
                "Quantity": st.column_config.NumberColumn("Quantity", help="Number of shares currently held."),
                "Purchase Price": st.column_config.NumberColumn("Purchase Price", help="Average price paid per share."),
                "Current Price": st.column_config.NumberColumn("Current Price", help="Latest market price per share from EasyEquities."),
                "Total Purchase Value": st.column_config.NumberColumn("Total Purchase Value", format="R %.2f", help="Quantity multiplied by the average purchase price."),
                "Current Value": st.column_config.NumberColumn("Current Value", format="R %.2f", help="Current market value of the holding."),
                "Gain/Loss": st.column_config.NumberColumn("Gain/Loss", format="R %.2f", help="Current value less total purchase value."),
                "Gain/Loss %": st.column_config.NumberColumn("Gain/Loss %", format="%.2f%%", help="Gain or loss as a percentage of total purchase value."),
                "Dividends": st.column_config.NumberColumn("Dividends", format="R %.2f", help="Total dividends recorded for this holding."),
            },
        )
    else:
        st.info("Select at least one column to display the holdings table.")
    # Export every column, not just the displayed selection.
    render_csv_download(
        holdings_table, f"portfolio_{account_type.lower().replace(' ', '_')}",
        key=f"portfolio_{account_type}_csv",
    )

    # Today's movement for this account group (JSE-priced symbols only -
    # USD-account holdings resolve to the wrong instrument via .JO lookups)
    symbols = [h.symbol for h in holdings if h.symbol and is_jse_tracked_account(h.account_type)]
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

        st.dataframe(rows, width="stretch", hide_index=True)

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
    """Page wrapper around services.easyequities_sync - stashes the raw
    holdings for the debug expander, then returns the synced count."""
    synced, raw = sync_portfolio_from_easyequities(db, portfolio_id, collector)
    st.session_state["ee_last_raw_holdings"] = raw
    return synced


def _add_holding(db: Session, portfolio_id: int, symbol: str, quantity: float, purchase_price: float, purchase_date) -> int | None:
    """Add a holding to the portfolio.

    Returns the number of price points backfilled, or None when the symbol is
    excluded and nothing was added.
    """
    if symbol in EXCLUDED_PORTFOLIO_SYMBOLS:
        return None

    # Check if stock exists
    stock = db.query(Stock).filter(Stock.symbol == symbol).first()
    
    if not stock:
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
    
    # Create holding. account_type="Manual" deliberately: EasyEquities
    # reconciliation deletes every holding under an account type it synced
    # that EE no longer reports - a manual holding under the default "ZAR"
    # account would be silently deleted on the next sync. "Manual" is never
    # an EE account type, so reconciliation skips it; is_jse_tracked_account
    # still treats it as JSE-tracked.
    holding = PortfolioHolding(
        portfolio_id=portfolio_id,
        stock_id=stock.id,
        symbol=symbol,
        account_type="Manual",
        quantity=quantity,
        purchase_price=purchase_price,
        purchase_date=purchase_date
    )
    
    db.add(holding)
    from app.services.transactions import record_transaction
    record_transaction(
        db,
        portfolio_id=portfolio_id, stock_id=stock.id, symbol=symbol,
        account_type="Manual", transaction_type="buy",
        quantity=quantity, price=purchase_price,
    )
    try:
        db.commit()
    except IntegrityError:
        # A concurrent writer created a conflicting row between our checks
        # and the commit - treat as "not added" rather than crashing.
        db.rollback()
        logger.warning(f"{symbol} holding insert hit a concurrent write")
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

    # Same bad-ticker rule as the watchlist add path: only deactivate when the
    # symbol has no stored prices at all; never on a mere provider outage.
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


def _remove_holding(
    db: Session,
    portfolio_id: int,
    symbol: str,
    account_type: str,
    record_sale: bool = True,
    sell_price: float | None = None,
    sold_at: datetime | None = None,
):
    """Remove a holding for a specific account type.

    `record_sale=True` logs the realized sale (at `sell_price`/`sold_at` when
    given, else the last known price, now). `record_sale=False` is a
    correction: the holding and the manual buys logged for it are deleted
    with no sale, so a typo doesn't leave phantom P&L behind.
    """
    from app.services.transactions import BUY, record_sale as log_sale

    holding = db.query(PortfolioHolding).filter(
        PortfolioHolding.portfolio_id == portfolio_id,
        PortfolioHolding.symbol == symbol,
        PortfolioHolding.account_type == (account_type or "ZAR"),
    ).first()
    if not holding:
        return
    if record_sale:
        if not sell_price:
            # Exit price: the holding's last known price, else the latest stored close.
            sell_price = holding.current_price
        if not sell_price:
            latest = db.query(StockPrice.close_price).filter(
                StockPrice.symbol == symbol
            ).order_by(StockPrice.timestamp.desc()).first()
            sell_price = latest.close_price if latest else holding.purchase_price
        log_sale(db, holding, sell_price, source="manual", sold_at=sold_at)
    else:
        # Only the buys logged for THIS holding (since it was created) - an
        # earlier, really-sold position in the same symbol keeps its history.
        buys = db.query(PortfolioTransaction).filter(
            PortfolioTransaction.portfolio_id == portfolio_id,
            PortfolioTransaction.symbol == symbol,
            PortfolioTransaction.account_type == (account_type or "ZAR"),
            PortfolioTransaction.transaction_type == BUY,
            PortfolioTransaction.source.in_(("manual", "backfill")),
        )
        if holding.created_at is not None:
            buys = buys.filter(
                PortfolioTransaction.created_at >= holding.created_at - timedelta(minutes=1)
            )
        buys.delete(synchronize_session=False)
    db.delete(holding)
    db.commit()


def _update_holding_values(db: Session, holdings: list) -> None:
    """Revalue holdings from stored prices (see services.portfolio_valuation),
    using this page's cached latest-price lookup."""
    update_holding_values(db, holdings, price_lookup=_get_group_latest_and_previous_prices)


def _update_portfolio_totals(db: Session, portfolio: Portfolio, holdings: list) -> None:
    """Recompute portfolio summary metrics and record today's value snapshot."""
    update_portfolio_totals(db, portfolio, holdings)
