"""Every dashboard page renders without an exception - with the network down.

Page code was 0-16% covered and both production crashes in Nov 2026 were
page-level (a VARCHAR overflow, a missing column after a hot reload). This
renders each page in Streamlit's AppTest against a seeded SQLite database
with all outbound connections refused, so it exercises the failure paths
users hit when a provider is down - and never touches real accounts.
"""
import socket
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from streamlit.testing.v1 import AppTest

from app.models import Base
from app.models.portfolio import Portfolio, PortfolioHolding
from app.models.stock import Stock, StockPrice
from app.models.watchlist import WatchlistItem

PAGES = [
    "home:_render_home", "watchlist:show_watchlist", "portfolio:show_portfolio",
    "dividends:show_dividends", "upcoming_ipos:show_upcoming_ipos",
    "notifications:show_notifications", "company:show_company",
    "penny_stocks:show_penny_stocks", "commodities:show_commodities",
    "krugerrands:show_krugerrands", "crypto:show_crypto", "settings_status:show_settings",
]
SCRIPT = str(Path(__file__).with_name("smoke_page_app.py"))


@pytest.fixture(scope="module")
def smoke_db(tmp_path_factory):
    url = f"sqlite:///{tmp_path_factory.mktemp('smoke') / 'smoke.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    stock = Stock(symbol="NPN", name="Naspers Limited", is_active=True)
    db.add(stock)
    db.flush()
    start = datetime(2026, 11, 20) - timedelta(days=60)
    for i in range(60):
        close = 600 + i
        db.add(StockPrice(stock_id=stock.id, symbol="NPN", timestamp=start + timedelta(days=i),
                          open_price=close, high_price=close + 5, low_price=close - 5,
                          close_price=close, adjusted_close=close, volume=1000, rsi=55.0))
    db.add(WatchlistItem(symbol="NPN", stock_id=stock.id, is_active=True, target_price=700.0))
    portfolio = Portfolio(name="Main")
    db.add(portfolio)
    db.flush()
    db.add(PortfolioHolding(portfolio_id=portfolio.id, stock_id=stock.id, symbol="NPN",
                            account_type="ZAR", quantity=10, purchase_price=550.0,
                            purchase_date=datetime(2026, 1, 5),
                            current_price=659.0, current_value=6590.0))
    db.commit()
    db.close()
    engine.dispose()
    return url


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Refuse every non-loopback connection (fast failure, no real accounts).

    Loopback stays open: Streamlit's runtime (asyncio on Windows) talks to
    itself over a local socket pair.
    """
    real_connect, real_create = socket.socket.connect, socket.create_connection
    local = ("127.0.0.1", "::1", "localhost")

    def guarded_connect(sock, address, *args, **kwargs):
        if isinstance(address, tuple) and address[0] not in local:
            raise ConnectionRefusedError(f"network disabled in page smoke tests ({address[0]})")
        return real_connect(sock, address, *args, **kwargs)

    def guarded_create(address, *args, **kwargs):
        if address[0] not in local:
            raise ConnectionRefusedError(f"network disabled in page smoke tests ({address[0]})")
        return real_create(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", guarded_create)

    # yfinance talks through curl_cffi (libcurl), below Python's socket
    # module - take Yahoo down at its entry points instead.
    import yfinance

    def yahoo_down(*args, **kwargs):
        raise ConnectionError("Yahoo disabled in page smoke tests")

    for name in ("download", "Ticker", "Search"):
        monkeypatch.setattr(yfinance, name, yahoo_down)
    # Bare-mode AppTest runs Streamlit's "are we in a REPL?" check on every
    # element - inspect.stack() over every loaded module, minutes on Windows
    # once yfinance/plotly are imported. Not app behaviour; skip it.
    monkeypatch.setattr("streamlit.env_util.is_repl", lambda: False)



@pytest.mark.parametrize("page", PAGES, ids=[p.split(":")[0] for p in PAGES])
def test_page_renders_without_exception(page, smoke_db, monkeypatch):
    monkeypatch.setenv("PAGE", page)
    monkeypatch.setenv("SMOKE_DB", smoke_db)
    at = AppTest.from_file(SCRIPT, default_timeout=120).run()
    assert not at.exception, [e.value for e in at.exception]
    # A rerun (any widget interaction) must not crash either.
    at.run()
    assert not at.exception, [e.value for e in at.exception]
