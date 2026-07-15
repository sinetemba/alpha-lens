from typing import Optional


def format_stock_label(symbol: str, name: Optional[str] = None) -> str:
    """Format a stock symbol with its company name, e.g. 'SOL - Sasol Limited'."""
    if name and name != symbol:
        return f"{symbol} - {name}"
    return symbol
