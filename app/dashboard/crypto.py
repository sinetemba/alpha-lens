import streamlit as st
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from typing import Optional

from app.collectors.coingecko import CoinGeckoCollector
from app.models.crypto import CryptoHolding

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
    st.caption("Track cryptocurrency holdings in ZAR via CoinGecko.")

    flash = st.session_state.pop("crypto_flash", None)
    if flash:
        level, message = flash
        getattr(st, level)(message)

    holdings = db.query(CryptoHolding).order_by(CryptoHolding.purchase_date).all()

    coin_ids = tuple(sorted({h.coin_id for h in holdings}))
    markets = _get_crypto_markets(coin_ids)

    if coin_ids and not markets:
        st.warning(
            "Couldn't fetch live prices from CoinGecko - showing cost "
            "basis only. Try again shortly (the free API is rate-limited)."
        )

    _render_holdings(db, holdings, markets)
    _render_market_snapshot(markets, coin_ids)


@st.cache_data(ttl=60, show_spinner=False)
def _get_crypto_markets(coin_ids: tuple) -> dict:
    """Live ZAR market data per coin id, cached 60s against API rate limits."""
    return CoinGeckoCollector().get_markets(list(coin_ids))


@st.cache_data(ttl=3600, show_spinner=False)
def _search_coin(query: str) -> list:
    """Resolve a name/symbol to CoinGecko coin ids (cached 1h)."""
    return CoinGeckoCollector().search(query)


def _render_holdings(db: Session, holdings: list, markets: dict):
    """Holdings table with live valuation plus add/remove flows."""
    st.subheader("My Holdings")

    if holdings:
        rows = []
        total_cost = total_value = 0.0
        has_cost = has_value = False
        for h in holdings:
            market = markets.get(h.coin_id) or {}
            price = market.get("current_price")
            change_24h = market.get("price_change_percentage_24h")
            current_value = price * h.quantity if price else None
            cost = (h.purchase_price or 0.0) * h.quantity
            has_cost = has_cost or cost > 0
            total_cost += cost
            if current_value is not None:
                has_value = True
                total_value += current_value
            gain = (current_value - cost) if current_value is not None and cost > 0 else None
            rows.append({
                "Coin": f"{h.symbol} — {h.name or h.coin_id}",
                "Qty": f"{h.quantity:g}",
                "Price (ZAR)": f"R {price:,.2f}" if price else "N/A",
                "24h": f"{change_24h:+.1f}%" if change_24h is not None else "-",
                "Paid per coin": f"R {h.purchase_price:,.2f}" if h.purchase_price else "-",
                "Current value": f"R {current_value:,.2f}" if current_value is not None else "N/A",
                "Gain/Loss": (
                    f"R {gain:+,.2f} ({gain / cost * 100:+.1f}%)" if gain is not None else "-"
                ),
                "Purchased": h.purchase_date.strftime("%Y-%m-%d") if h.purchase_date else "-",
            })

        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("Total cost", f"R {total_cost:,.2f}" if has_cost else "N/A")
        with c2:
            st.metric("Current value", f"R {total_value:,.2f}" if has_value else "N/A")
        with c3:
            if has_cost and has_value:
                gain = total_value - total_cost
                st.metric("Gain/Loss", f"R {gain:+,.2f}", delta=f"{gain / total_cost * 100:+.1f}%")
            else:
                st.metric("Gain/Loss", "N/A")

        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info("No crypto holdings yet - record a purchase below.")

    _render_add_form(db)

    if holdings:
        remove_options = {
            f"{h.quantity:g} {h.symbol} — {h.name or h.coin_id} (#{h.id})": h.id
            for h in holdings
        }
        with st.expander("🗑️ Remove a holding"):
            to_remove = st.multiselect(
                "Select holdings to remove",
                list(remove_options.keys()),
                key="crypto_remove_select",
            )
            if st.button("Remove selected", key="crypto_remove_btn", disabled=not to_remove):
                ids = {remove_options[k] for k in to_remove}
                for h in holdings:
                    if h.id in ids:
                        db.delete(h)
                db.commit()
                st.session_state["crypto_flash"] = ("success", f"Removed {len(ids)} holding(s).")
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
            "Price (ZAR)": f"R {row['current_price']:,.2f}" if row.get("current_price") else "N/A",
            "24h": (
                f"{row['price_change_percentage_24h']:+.1f}%"
                if row.get("price_change_percentage_24h") is not None else "-"
            ),
            "Market cap": (
                f"R {row['market_cap'] / 1e9:,.1f}bn" if row.get("market_cap") else "-"
            ),
        } for coin_id, row in snapshot.items()],
        use_container_width=True,
        hide_index=True,
    )
