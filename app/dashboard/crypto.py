import streamlit as st
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.collectors.coingecko import CoinGeckoCollector
from app.collectors.luno import LunoCollector, luno_asset_for
from app.dashboard.utils import _to_display_tz, holding_totals, price_column_format, render_holding_totals
from app.models.base import utcnow_naive
from app.models.crypto import CryptoBundle, CryptoHolding
from app.services.luno_sync import LUNO_SOURCE, last_luno_sync, sync_luno_holdings

# Luno balances are re-synced on page load when the last sync is older than this.
LUNO_SYNC_MAX_AGE = timedelta(minutes=15)

# CoinGecko ids for the popular-coins picker. Anything else can be added by
# CoinGecko id via the "Other" option - the id is validated against the API
# on save.
POPULAR_COINS = {
    "bitcoin": "Bitcoin (BTC)",
    "ethereum": "Ethereum (ETH)",
    "solana": "Solana (SOL)",
    "ripple": "XRP (XRP)",
    "cardano": "Cardano (ADA)",
    "dogecoin": "Dogecoin (DOGE)",
    "tron": "TRON (TRX)",
    "avalanche-2": "Avalanche (AVAX)",
    "chainlink": "Chainlink (LINK)",
    "polkadot": "Polkadot (DOT)",
    "litecoin": "Litecoin (LTC)",
    "bitcoin-cash": "Bitcoin Cash (BCH)",
    "stellar": "Stellar (XLM)",
    "uniswap": "Uniswap (UNI)",
    "the-open-network": "Toncoin (TON)",
}


def show_crypto(db: Session):
    """Display the cryptocurrency holdings tracking page."""
    st.header("₿ Crypto")
    st.caption(
        "Luno holdings sync automatically and are valued at Luno's ZAR bid; coins held "
        "elsewhere can be recorded manually and are valued via CoinGecko."
    )

    flash = st.session_state.pop("crypto_flash", None)
    if flash:
        level, message = flash
        getattr(st, level)(message)

    _render_luno_sync(db)

    holdings = db.query(CryptoHolding).order_by(
        CryptoHolding.source, CryptoHolding.purchase_date
    ).all()

    # CoinGecko for manual rows' prices and everyone's 24h change; Luno-only
    # assets ("luno-*" keys) have no CoinGecko id.
    coin_ids = tuple(sorted({h.coin_id for h in holdings if not h.coin_id.startswith("luno-")}))
    markets = _get_crypto_markets(coin_ids)
    has_luno = any(h.source == LUNO_SOURCE for h in holdings)
    luno_prices = _get_luno_prices() if has_luno else {}

    if any(h.source != LUNO_SOURCE for h in holdings) and coin_ids and not markets:
        st.warning(
            "Couldn't fetch live prices from CoinGecko - manual holdings show cost "
            "basis only. Try again shortly (the free API is rate-limited)."
        )
    if has_luno and luno_prices is None:
        st.warning("Couldn't fetch Luno prices - Luno holdings show cost basis only.")

    _render_holdings(db, holdings, markets, luno_prices or {})
    _render_market_snapshot(markets, coin_ids)


def _render_luno_sync(db: Session) -> None:
    """Luno connection status, auto-sync when stale, and a manual sync button."""
    collector = LunoCollector()
    if not collector.enabled:
        st.info(
            "**Connect Luno** to sync your crypto balances automatically: create a "
            "**read-only** API key at luno.com/wallet/security/api_keys (balance view only - "
            "no trading or withdrawal permission), then add `LUNO_API_KEY_ID` and "
            "`LUNO_API_SECRET` to `.env` and restart the dashboard."
        )
        return

    last = last_luno_sync(db)
    stale = last is None or utcnow_naive() - last > LUNO_SYNC_MAX_AGE
    # Auto-sync at most once per session while stale, so a failing API
    # doesn't retry on every widget interaction.
    auto = stale and not st.session_state.get("luno_auto_sync_tried")

    col1, col2 = st.columns([3, 1])
    with col2:
        manual = st.button("🔄 Sync from Luno", key="luno_sync_btn")
    if manual or auto:
        st.session_state["luno_auto_sync_tried"] = True
        with st.spinner("Syncing balances from Luno..."):
            result = sync_luno_holdings(db, collector)
        if result is None:
            st.error(f"Luno sync failed: {collector.last_error}")
        else:
            st.session_state["luno_fiat"] = result.fiat
            st.session_state["luno_cost_error"] = result.cost_error
            _get_luno_prices.clear()
            if manual:
                st.success(
                    f"Synced {result.synced} Luno holding(s)"
                    + (f", removed {result.removed} no longer held" if result.removed else "")
                    + "."
                )
        last = last_luno_sync(db)

    with col1:
        when = (
            _to_display_tz(last).strftime("%Y-%m-%d %H:%M") + " SAST" if last else "never"
        )
        fiat = st.session_state.get("luno_fiat") or {}
        cash = ", ".join(f"{cur} {amount:,.2f}" for cur, amount in sorted(fiat.items()))
        st.caption(f"Luno: last synced {when}" + (f" · cash on Luno: {cash}" if cash else ""))
    cost_error = st.session_state.get("luno_cost_error")
    if cost_error:
        st.warning(
            f"Couldn't read your Luno transaction history, so purchase costs weren't "
            f"updated: {cost_error}. Costs you entered by hand are unaffected."
        )


@st.cache_data(ttl=60, show_spinner=False)
def _get_luno_prices() -> Optional[dict]:
    """Luno ZAR bid per asset (public tickers), cached 60s."""
    return LunoCollector().get_zar_prices()


@st.cache_data(ttl=60, show_spinner=False)
def _get_crypto_markets(coin_ids: tuple) -> dict:
    """Live ZAR market data per coin id, cached 60s against API rate limits."""
    return CoinGeckoCollector().get_markets(list(coin_ids))


@st.cache_data(ttl=3600, show_spinner=False)
def _search_coin(query: str) -> list:
    """Resolve a name/symbol to CoinGecko coin ids (cached 1h)."""
    return CoinGeckoCollector().search(query)


def _holding_valuation(holding: CryptoHolding, price: Optional[float]) -> tuple:
    """(cost, current_value) in ZAR; None where unknown (no price paid / no quote)."""
    cost = holding.purchase_price * holding.quantity if holding.purchase_price else None
    current_value = price * holding.quantity if price else None
    return cost, current_value


def _holding_price(holding: CryptoHolding, markets: dict, luno_prices: dict) -> Optional[float]:
    """ZAR price per coin: Luno's bid for Luno rows, CoinGecko otherwise.

    A Luno coin with no Luno ZAR route at all falls back to CoinGecko rather
    than showing no value.
    """
    gecko = (markets.get(holding.coin_id) or {}).get("current_price")
    if holding.source == LUNO_SOURCE:
        return luno_prices.get(luno_asset_for(holding.coin_id)) or gecko
    return gecko


def _bundle_value(coins: list, markets: dict, luno_prices: dict) -> Optional[float]:
    """Combined ZAR value of a bundle's coins; None if no coin could be priced."""
    values = [
        _holding_valuation(h, _holding_price(h, markets, luno_prices))[1] for h in coins
    ]
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _render_holdings(db: Session, holdings: list, markets: dict, luno_prices: dict):
    """Holdings table with live valuation plus add/remove flows.

    Coins held directly (wallets, manual records) are listed one per row;
    Luno Bundle coins are grouped under their bundle and count as a single
    holding in the totals.
    """
    st.subheader("My Holdings")

    direct = [h for h in holdings if not h.bundle]
    bundles: dict[str, list] = {}
    for h in holdings:
        if h.bundle:
            bundles.setdefault(h.bundle, []).append(h)
    bundle_records = {
        b.name: b for b in db.query(CryptoBundle).filter(CryptoBundle.name.in_(list(bundles)))
    } if bundles else {}

    if holdings:
        rows = []
        # Each bundle is ONE (cost, value) pair: what was paid for the bundle
        # against its combined value.
        valued = [
            (
                (bundle_records[name].purchase_cost if name in bundle_records else None) or None,
                _bundle_value(coins, markets, luno_prices),
            )
            for name, coins in bundles.items()
        ]
        for h in direct:
            market = markets.get(h.coin_id) or {}
            price = _holding_price(h, markets, luno_prices)
            change_24h = market.get("price_change_percentage_24h")
            cost, current_value = _holding_valuation(h, price)
            valued.append((cost, current_value))
            gain = (current_value - cost) if current_value is not None and cost else None
            rows.append({
                "Coin": f"{h.symbol} — {h.name or h.coin_id}",
                "Source": "Luno" if h.source == LUNO_SOURCE else "Manual",
                "Qty": h.quantity,
                "Price (ZAR)": price,
                "24h %": change_24h,
                "Paid per coin": h.purchase_price,
                "Current value": current_value,
                "Gain/Loss": gain,
                "Gain %": gain / cost * 100 if gain is not None else None,
                "Purchased": h.purchase_date.strftime("%Y-%m-%d") if h.purchase_date else "-",
            })

        render_holding_totals(holding_totals(valued))
        if rows:
            _render_direct_table(rows)
        for name, coins in bundles.items():
            _render_bundle(db, name, coins, bundle_records.get(name), markets, luno_prices)
        if any(h.source == LUNO_SOURCE and not h.purchase_price for h in direct):
            st.caption(
                "Some Luno holdings have no purchase cost in their Luno history (e.g. coins "
                "received rather than bought) - set a cost per coin below to see their gain/loss."
            )
        if any(h.cost_source == "luno" for h in direct):
            st.caption("Paid per coin = average cost of the coins you hold, from your Luno trade history.")
    else:
        st.info("No crypto holdings yet - connect Luno or record a purchase below.")

    _render_luno_cost_basis(db, [h for h in direct if h.source == LUNO_SOURCE])
    _render_add_form(db)
    _render_remove_manual(db, [h for h in holdings if h.source != LUNO_SOURCE])


def _render_direct_table(rows: list) -> None:
    """Coins held directly - Luno wallets and manual records, one row each."""
    price_fmt = price_column_format(
        [r["Price (ZAR)"] for r in rows], [r["Paid per coin"] for r in rows]
    )
    # Numeric cells (not "R x" strings) so column sorting works.
    st.dataframe(
        rows, use_container_width=True, hide_index=True,
        column_config={
            "Qty": st.column_config.NumberColumn(format="%g"),
            "Price (ZAR)": st.column_config.NumberColumn(format=price_fmt),
            "24h %": st.column_config.NumberColumn(format="%+.1f%%"),
            "Paid per coin": st.column_config.NumberColumn(format=price_fmt),
            "Current value": st.column_config.NumberColumn(format="R %.2f"),
            "Gain/Loss": st.column_config.NumberColumn(format="R %+.2f"),
            "Gain %": st.column_config.NumberColumn(format="%+.1f%%"),
        },
    )


def _render_bundle(
    db: Session, name: str, coins: list, record: Optional[CryptoBundle],
    markets: dict, luno_prices: dict,
) -> None:
    """A Luno Bundle as one holding: value, cost, gain and its coin breakdown."""
    priced = []
    for h in coins:
        price = _holding_price(h, markets, luno_prices)
        priced.append((h, price, _holding_valuation(h, price)[1]))
    total = sum(v for _, _, v in priced if v is not None)
    cost = record.purchase_cost if record else None

    title = (record.label if record and record.label else name)
    st.markdown(f"**📦 {title}**")
    c1, c2, c3 = st.columns(3)
    c1.metric("Bundle value", f"R {total:,.2f}" if total else "N/A")
    c2.metric(
        "Paid for bundle", f"R {cost:,.2f}" if cost else "Not set",
        help="Net Rand invested, from your Luno transaction history"
        if record and record.cost_source == "luno" else "Entered by you",
    )
    if cost and total:
        gain = total - cost
        c3.metric("Gain/Loss", f"R {gain:+,.2f}", delta=f"{gain / cost * 100:+.1f}%")
    else:
        c3.metric("Gain/Loss", "N/A")

    breakdown = sorted(
        (
            {
                "Coin": f"{h.symbol} — {h.name or h.coin_id}",
                "Weight %": value / total * 100 if value is not None and total else None,
                "Value": value,
                "Qty": h.quantity,
                "Price (ZAR)": price,
            }
            for h, price, value in priced
        ),
        key=lambda r: r["Value"] or 0,
        reverse=True,
    )
    st.dataframe(
        breakdown, use_container_width=True, hide_index=True,
        column_config={
            "Weight %": st.column_config.NumberColumn(format="%.2f%%"),
            "Value": st.column_config.NumberColumn(format="R %.2f"),
            "Qty": st.column_config.NumberColumn(format="%g"),
            "Price (ZAR)": st.column_config.NumberColumn(
                format=price_column_format([r["Price (ZAR)"] for r in breakdown])
            ),
        },
    )

    with st.expander(f"✏️ Set what you paid for {name}"):
        paid = st.number_input(
            "Total paid for the bundle (R)", min_value=0.0, value=float(cost or 0.0),
            step=100.0, key=f"bundle_cost_{name}",
            help="The Rand amount you invested in the bundle (Luno's Transactions history "
                 "shows it). Top-ups: enter the running total. Set 0 to clear.",
        )
        save_col, reset_col = st.columns(2)
        if save_col.button("Save bundle cost", key=f"bundle_cost_save_{name}"):
            record = record or db.query(CryptoBundle).filter_by(name=name).first()
            if record is None:
                record = CryptoBundle(name=name)
                db.add(record)
            record.purchase_cost = paid or None
            # Manual entries win over the Luno-history figure on later syncs.
            record.cost_source = "manual" if paid else None
            db.commit()
            st.session_state["crypto_flash"] = ("success", f"Saved cost for {title}.")
            st.rerun()
        if record and record.cost_source == "manual" and reset_col.button(
            "Use Luno history instead", key=f"bundle_cost_reset_{name}"
        ):
            record.cost_source = None
            db.commit()
            _resync_costs(db)


def _render_remove_manual(db: Session, manual: list) -> None:
    """Remove manual records (two-step confirm). Luno rows follow the balance."""
    if not manual:
        return
    remove_options = {
        f"{h.quantity:g} {h.symbol} — {h.name or h.coin_id} (#{h.id})": h.id
        for h in manual
    }
    with st.expander("🗑️ Remove a manual holding"):
        st.caption("Luno holdings follow your Luno balance and can't be removed here.")
        to_remove = st.multiselect(
            "Select holdings to remove",
            list(remove_options.keys()),
            key="crypto_remove_select",
        )
        remove_key = "crypto_remove_pending"
        if st.button("Remove selected", key="crypto_remove_btn", disabled=not to_remove):
            st.session_state[remove_key] = tuple(to_remove)

        pending = st.session_state.get(remove_key)
        if pending:
            st.warning(
                f"Remove {len(pending)} holding(s) ({', '.join(pending)})? "
                "This deletes the purchase record(s)."
            )
            confirm_col, cancel_col = st.columns(2)
            with confirm_col:
                if st.button("Yes, remove", key="crypto_confirm_remove"):
                    ids = {remove_options[k] for k in pending if k in remove_options}
                    for h in manual:
                        if h.id in ids:
                            db.delete(h)
                    db.commit()
                    st.session_state[remove_key] = None
                    st.session_state["crypto_flash"] = ("success", f"Removed {len(ids)} holding(s).")
                    st.rerun()
            with cancel_col:
                if st.button("Cancel", key="crypto_cancel_remove"):
                    st.session_state[remove_key] = None
                    st.rerun()


def _render_luno_cost_basis(db: Session, luno_holdings: list) -> None:
    """Enter the price paid per coin for Luno-synced holdings (kept across syncs)."""
    if not luno_holdings:
        return
    with st.expander("✏️ Set cost per coin (Luno holdings)"):
        by_id = {h.id: h for h in luno_holdings}
        holding_id = st.selectbox(
            "Holding",
            list(by_id),
            format_func=lambda i: f"{by_id[i].quantity:g} {by_id[i].symbol} — {by_id[i].name}",
            key="luno_cost_holding",
        )
        current = by_id[holding_id].purchase_price or 0.0
        price = st.number_input(
            "Average price paid per coin (R)", min_value=0.0, value=float(current),
            step=100.0, key=f"luno_cost_price_{holding_id}",
            help="Your average buy price. Luno's Transactions history shows what you paid. "
                 "Set 0 to clear.",
        )
        save_col, reset_col = st.columns(2)
        if reset_col.button(
            "Use Luno history instead", key="luno_cost_reset",
            disabled=by_id[holding_id].cost_source != "manual",
        ):
            by_id[holding_id].cost_source = None
            db.commit()
            _resync_costs(db)
        if save_col.button("Save cost", key="luno_cost_save"):
            by_id[holding_id].purchase_price = price or None
            # Manual entries win over the Luno-history figure on later syncs.
            by_id[holding_id].cost_source = "manual" if price else None
            db.commit()
            st.session_state["crypto_flash"] = (
                "success", f"Saved cost basis for {by_id[holding_id].symbol}."
            )
            st.rerun()


def _resync_costs(db: Session) -> None:
    """Re-derive costs from Luno history now (after clearing a manual override)."""
    result = sync_luno_holdings(db, LunoCollector())
    if result is None or result.cost_error:
        st.session_state["crypto_flash"] = (
            "warning", "Couldn't read Luno history - the cost will refresh on the next sync."
        )
    else:
        st.session_state["crypto_flash"] = ("success", "Cost restored from your Luno history.")
    st.rerun()


def _resolve_coin(selection: str, custom_id: str) -> Optional[dict]:
    """Resolve the add-form selection to a CoinGecko coin dict, or None."""
    if selection != "__other__":
        label = POPULAR_COINS[selection]
        name, _, symbol = label.partition("(")
        return {
            "id": selection,
            "name": name.strip(),
            "symbol": symbol.rstrip(")"),
        }

    coin_id = custom_id.strip().lower()
    if not coin_id:
        return None
    # Validate the id actually resolves to a priced coin.
    markets = _get_crypto_markets(tuple([coin_id]))
    if markets.get(coin_id):
        row = markets[coin_id]
        return {"id": coin_id, "name": row.get("name", coin_id), "symbol": (row.get("symbol") or coin_id).upper()}
    return None


def _render_add_form(db: Session):
    """Add form: pick a popular coin or search CoinGecko for anything else."""
    with st.expander("➕ Record a purchase"):
        options = list(POPULAR_COINS.keys()) + ["__other__"]
        selection = st.selectbox(
            "Coin",
            options,
            format_func=lambda k: "Other (search CoinGecko)..." if k == "__other__" else POPULAR_COINS[k],
            key="crypto_add_coin",
        )

        custom_id = ""
        resolved_preview = None
        if selection == "__other__":
            query = st.text_input(
                "Search CoinGecko (name or symbol)",
                key="crypto_add_search",
                placeholder="e.g. near, PEPE, Injective",
            )
            candidates = _search_coin(query) if query else []
            if candidates:
                pick = st.selectbox(
                    "Match",
                    candidates,
                    format_func=lambda c: f"{c['symbol']} — {c['name']} ({c['id']})",
                    key="crypto_add_match",
                )
                custom_id = pick["id"]
                resolved_preview = pick
            elif query:
                st.caption("No matches - check the spelling or try the exact CoinGecko id.")

        col1, col2 = st.columns(2)
        with col1:
            quantity = st.number_input(
                "Quantity", min_value=0.0, value=0.0, step=0.001, format="%.8f", key="crypto_add_qty"
            )
            purchase_price = st.number_input(
                "Price paid per coin (R)", min_value=0.0, value=0.0, step=100.0, key="crypto_add_price",
                help="Leave 0 if unknown - value tracking still works without a cost basis.",
            )
        with col2:
            purchase_date = st.date_input(
                "Purchase date", value=datetime.now(timezone.utc).date(), key="crypto_add_date"
            )
            notes = st.text_input("Notes (optional)", key="crypto_add_notes")

        if st.button("Save purchase", key="crypto_add_btn"):
            if quantity <= 0:
                st.session_state["crypto_flash"] = ("error", "Quantity must be greater than 0.")
            else:
                coin = resolved_preview or _resolve_coin(selection, custom_id)
                if not coin:
                    st.session_state["crypto_flash"] = (
                        "error", "Couldn't resolve that coin on CoinGecko - check the selection."
                    )
                else:
                    db.add(CryptoHolding(
                        coin_id=coin["id"],
                        symbol=coin["symbol"],
                        name=coin["name"],
                        quantity=quantity,
                        purchase_price=purchase_price or None,
                        purchase_date=datetime.combine(purchase_date, datetime.min.time()),
                        notes=notes or None,
                    ))
                    db.commit()
                    st.session_state["crypto_flash"] = (
                        "success", f"Recorded {quantity:g} {coin['symbol']} ({coin['name']})."
                    )
            st.rerun()


def _render_market_snapshot(markets: dict, held_ids: tuple):
    """Live prices for held coins (and majors when nothing is held yet)."""
    st.subheader("Prices")
    show_ids = held_ids or ("bitcoin", "ethereum", "solana")
    snapshot = markets if held_ids else _get_crypto_markets(tuple(show_ids))

    if not snapshot:
        st.info("No live prices available right now.")
        return

    st.dataframe(
        [{
            "Coin": f"{(row.get('symbol') or coin_id).upper()} — {row.get('name', coin_id)}",
            "Price (ZAR)": row.get("current_price"),
            "24h %": row.get("price_change_percentage_24h"),
            "Market cap (R bn)": row["market_cap"] / 1e9 if row.get("market_cap") else None,
        } for coin_id, row in snapshot.items()],
        use_container_width=True,
        hide_index=True,
        column_config={
            "Price (ZAR)": st.column_config.NumberColumn(format="R %.2f"),
            "24h %": st.column_config.NumberColumn(format="%+.1f%%"),
            "Market cap (R bn)": st.column_config.NumberColumn(format="%.1f"),
        },
    )
