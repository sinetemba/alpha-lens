# AlphaLens App Review

Comprehensive review of the AlphaLens JSE Stock Analysis Platform — gaps, bugs, improvements, and feature roadmap.

---

## 1. Critical Bugs & Issues

### 🔴 Portfolio refresh still broken (root cause)

The `show_portfolio` function in `app/dashboard/portfolio.py` calls `_sync_portfolio_from_easyequities()` on **every page load** (not just on refresh), but the `updated_at` timestamp on holdings uses `datetime.utcnow` as a default/onupdate — it only changes when SQLAlchemy detects a column mutation. If EasyEquities returns the same values, no columns actually change, so `updated_at` stays stale and the caption shows the old date.

### 🔴 Missing `timedelta` import

`app/dashboard/portfolio.py` — `_get_price_movements()` uses `timedelta` but only `datetime` is imported. This function will crash if called.

### 🔴 `_get_price_movements` is dead code

The function exists in `app/dashboard/portfolio.py` (lines 420–467) but is never called anywhere.

### 🔴 JSE Index is hard-coded

`app/dashboard/home.py` — `_get_jse_index()` and `_get_jse_change()` return static placeholder values (`"78,450"` and `"+1.2%"`). The Home page always shows fake data.

### 🔴 Daily Gain is actually total gain

`app/dashboard/home.py` — `_get_daily_gain()` sums **all-time** gain/loss across all holdings, not the daily change. The label says "Daily Gain" which is misleading.

### 🟡 Deprecated SQLAlchemy API

`app/models/base.py` uses `declarative_base()` from `sqlalchemy.ext.declarative`, which is deprecated. Should use `sqlalchemy.orm.DeclarativeBase`.

### 🟡 `datetime.utcnow()` is deprecated

Used throughout all models. Python 3.12+ deprecates it in favour of `datetime.now(timezone.utc)`.

---

## 2. Testing — Zero Coverage

The `tests/` directory is **completely empty**. Despite having `pytest`, `pytest-asyncio`, and `pytest-cov` in `requirements.txt`, there are no tests at all. This is the single biggest gap.

**Recommended test priorities:**

- **EasyEquities sync logic** — holding aggregation, exclusions, purchase value calculations
- **Technical indicator calculations** — known input → expected output
- **Data service** — price saving deduplication, historical data backfill
- **Notification alert logic** — price alerts, technical crossovers

---

## 3. Security Gaps

- **No authentication** — anyone with access to the URL can view/modify portfolio data
- **EasyEquities credentials stored in plaintext** in `.env` — acceptable for local use but a risk if deployed
- **API keys in `.env.example`** — check git history for accidentally committed keys; rotate if needed
- **No CSRF/input sanitization** — Streamlit handles this somewhat, but symbol inputs are passed directly to API calls without validation
- **No rate limiting on the UI** — rapid button clicks can fire many concurrent API calls

---

## 4. Architecture & Code Quality

### Database

- **No migration management** — Alembic is configured but there's no evidence of migration files being used. Schema changes require manual table drops.
- **Single-user design** — no user model or multi-tenancy; the single `Portfolio` row is shared.
- **No foreign key enforcement** for EasyEquities-synced holdings — if a symbol doesn't match an existing `Stock`, a bare record is created without sector/industry info.

### Error Handling

- Many exceptions are silently caught and logged, with no user feedback. Example: `_add_holding` catches the price backfill failure silently.
- No retry logic for transient API failures (Twelve Data, Yahoo Finance, EasyEquities).

### Performance

- **N+1 queries everywhere** — the watchlist summary, portfolio, and home page loop through items and fire individual DB queries per symbol.
- **No caching** — `diskcache` is in requirements but never used; every page load re-queries everything.
- **EasyEquities logs in on every page load** — this is slow and could trigger rate limiting/account locks.
- **Technical indicators recalculated on every price update** for the entire history, not just the new points.

---

## 5. Missing Features (High Value)

| Feature | Why It Matters |
|---|---|
| **Dividend tracking & income page** | Model exists but no UI; dividends are never fetched or displayed |
| **Portfolio performance over time** | No historical portfolio value snapshots — can't chart portfolio growth |
| **Transaction history** | No buy/sell log; only current holdings are tracked |
| **Sector/industry allocation chart** | Data exists in `Stock` model but never visualized |
| **Export to CSV/Excel** | No way to export portfolio or watchlist data |
| **Notifications UI page** | Notification model & service exist but no Streamlit page to view/manage them |
| **Multi-portfolio support** | Model supports it but UI only uses `.first()` |
| **Watchlist price charts** | Individual stock detail from watchlist (link to Company View) |
| **Search/add by company name** | Currently requires knowing the exact JSE ticker symbol |
| **Currency conversion display** | ZAR amounts shown but no toggle for USD equivalent |

---

## 6. UX Improvements

- **No loading states** for initial page renders — blank page until DB queries complete
- **No empty-state illustrations** — just plain `st.info()` messages
- **Sidebar has no active-page indicator styling**
- **No dark mode consideration** — hard-coded background colors in portfolio gain/loss styling
- **Column multiselect on portfolio resets on every rerun** (Streamlit default behaviour)
- **No pagination** on historical data table (loads 100 rows at once)
- **No confirmation dialog** before removing holdings

---

## 7. DevOps & Deployment

- **Dockerfile installs Playwright but it's not used anywhere** — unnecessary image bloat (~400MB)
- **No CI/CD pipeline** (no GitHub Actions, no pre-commit hooks)
- **No linting config** (no `ruff.toml`, `pyproject.toml`, or `.flake8`)
- **`asyncio` package in requirements** — this is a stdlib module; the pip package is unmaintained and unnecessary
- **No `.streamlit/config.toml`** for theme/server configuration

---

## 8. Recommended Next Steps (Priority Order)

1. **Fix portfolio refresh root cause** — force `updated_at = datetime.utcnow()` on every sync, not just when values change
2. **Fix the JSE index placeholder** — fetch real J203 data via Yahoo Finance
3. **Fix daily gain calculation** — compare today's values vs yesterday's
4. **Add basic tests** — start with the data service and portfolio sync logic
5. **Cache EasyEquities login** — use `st.session_state` to avoid re-authenticating on every rerun
6. **Build a Notifications page** — the backend service is complete but invisible to the user
7. **Build a Dividends page** — the model exists; pull dividend data from Yahoo Finance
8. **Add portfolio value snapshots** — daily snapshot job in scheduler to enable performance-over-time charts
9. **Add CSV export** for portfolio and watchlist
10. **Remove Playwright from Dockerfile** — it's unused
