"""Streamlit-free display formatting shared by the dashboard and the updater.

Lives outside app.dashboard so headless code (notifications, data_service)
can format prices without importing Streamlit.
"""
from typing import Optional

import pandas as pd

# Prices below this are shown with 4 decimals - "R 0.01" for a R0.0085
# penny stock hides the price entirely.
SUB_RAND = 1.0


def format_price(value: Optional[float], na: str = "N/A") -> str:
    """Per-share Rand price: 2 decimals, or 4 for sub-Rand prices."""
    if value is None or pd.isna(value):
        return na
    if 0 < abs(value) < SUB_RAND:
        return f"R {value:,.4f}"
    return f"R {value:,.2f}"
