import pandas as pd
import numpy as np
from loguru import logger


class TechnicalIndicators:
    """Calculate technical analysis indicators for stock price data."""
    
    @staticmethod
    def calculate_sma(data: pd.Series, period: int) -> pd.Series:
        """Calculate Simple Moving Average."""
        return data.rolling(window=period).mean()
    
    @staticmethod
    def calculate_ema(data: pd.Series, period: int) -> pd.Series:
        """Calculate Exponential Moving Average."""
        return data.ewm(span=period, adjust=False).mean()
    
    @staticmethod
    def calculate_rsi(data: pd.Series, period: int = 14) -> pd.Series:
        """Calculate Relative Strength Index using Wilder's smoothing.

        ewm(alpha=1/period) matches the recursive averaging in Wilder's
        original definition - a plain rolling mean overstates swings and
        disagrees with published RSI values.
        """
        delta = data.diff()
        gain = delta.where(delta > 0, 0).ewm(alpha=1 / period, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1 / period, adjust=False).mean()

        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))

        return rsi
    
    @staticmethod
    def calculate_macd(data: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
        """Calculate MACD (Moving Average Convergence Divergence)."""
        ema_fast = data.ewm(span=fast, adjust=False).mean()
        ema_slow = data.ewm(span=slow, adjust=False).mean()
        
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line
        
        return {
            "macd": macd_line,
            "signal": signal_line,
            "histogram": histogram
        }
    
    @staticmethod
    def calculate_bollinger_bands(data: pd.Series, period: int = 20, std_dev: float = 2) -> dict:
        """Calculate Bollinger Bands."""
        sma = data.rolling(window=period).mean()
        std = data.rolling(window=period).std()
        
        upper_band = sma + (std * std_dev)
        lower_band = sma - (std * std_dev)
        
        return {
            "upper": upper_band,
            "middle": sma,
            "lower": lower_band
        }
    
    @staticmethod
    def calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
        """Calculate Average True Range."""
        high_low = high - low
        high_close = np.abs(high - close.shift())
        low_close = np.abs(low - close.shift())
        
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        # Wilder's smoothing, consistent with the RSI definition.
        atr = true_range.ewm(alpha=1 / period, adjust=False).mean()

        return atr
    
    @staticmethod
    def calculate_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
        """Calculate On-Balance Volume."""
        obv = (np.sign(close.diff()) * volume).fillna(0).cumsum()
        return obv
    
    @staticmethod
    def calculate_vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
        """Cumulative VWAP anchored at the loaded frame's first bar.

        Not a session VWAP (bars are daily) and not rolling - the anchor is
        the first bar of whatever range was fed in, so the value only moves
        when the window does. Renamed here but stored as `vwap` for
        compatibility.
        """
        typical_price = (high + low + close) / 3
        vwap = (typical_price * volume).cumsum() / volume.cumsum()
        return vwap
    
    @staticmethod
    def calculate_stochastic(high: pd.Series, low: pd.Series, close: pd.Series, k_period: int = 14, d_period: int = 3) -> dict:
        """Calculate Stochastic Oscillator."""
        low_min = low.rolling(window=k_period).min()
        high_max = high.rolling(window=k_period).max()
        
        k_percent = 100 * ((close - low_min) / (high_max - low_min))
        d_percent = k_percent.rolling(window=d_period).mean()
        
        return {
            "k": k_percent,
            "d": d_percent
        }
    
    @staticmethod
    def calculate_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> dict:
        """Calculate Average Directional Index."""
        high_diff = high.diff()
        low_diff = -low.diff()
        
        plus_dm = np.where((high_diff > low_diff) & (high_diff > 0), high_diff, 0)
        minus_dm = np.where((low_diff > high_diff) & (low_diff > 0), low_diff, 0)
        
        tr = pd.concat([
            high - low,
            np.abs(high - close.shift()),
            np.abs(low - close.shift())
        ], axis=1).max(axis=1)
        
        # Wilder smoothing (ewm alpha=1/n), the ADX definition's own moving
        # average - a plain SMA underreacts and disagrees with the RSI/ATR
        # paths, which are already Wilder. Keep the frame's index on the
        # +DM/-DM series so they can't misalign against tr.
        def _wilder(s: pd.Series) -> pd.Series:
            return s.ewm(alpha=1.0 / period, adjust=False).mean()

        plus_dm = _wilder(pd.Series(plus_dm, index=high.index))
        minus_dm = _wilder(pd.Series(minus_dm, index=high.index))
        atr = _wilder(tr)

        plus_di = 100 * (plus_dm / atr)
        minus_di = 100 * (minus_dm / atr)

        dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
        adx = _wilder(dx)
        
        return {
            "adx": adx,
            "plus_di": plus_di,
            "minus_di": minus_di
        }
    
    @staticmethod
    def calculate_cci(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 20) -> pd.Series:
        """Calculate Commodity Channel Index."""
        typical_price = (high + low + close) / 3
        sma = typical_price.rolling(window=period).mean()
        mean_deviation = typical_price.rolling(window=period).apply(lambda x: np.abs(x - x.mean()).mean())
        
        cci = (typical_price - sma) / (0.015 * mean_deviation)
        
        return cci
    
    @staticmethod
    def calculate_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
        """Calculate all technical indicators for a DataFrame.
        
        Expected columns: open, high, low, close, volume
        """
        result_df = df.copy()
        
        try:
            # SMA
            result_df['sma_20'] = TechnicalIndicators.calculate_sma(result_df['close'], 20)
            result_df['sma_50'] = TechnicalIndicators.calculate_sma(result_df['close'], 50)
            result_df['sma_200'] = TechnicalIndicators.calculate_sma(result_df['close'], 200)
            
            # EMA
            result_df['ema_12'] = TechnicalIndicators.calculate_ema(result_df['close'], 12)
            result_df['ema_26'] = TechnicalIndicators.calculate_ema(result_df['close'], 26)
            
            # RSI
            result_df['rsi'] = TechnicalIndicators.calculate_rsi(result_df['close'], 14)
            
            # MACD
            macd_data = TechnicalIndicators.calculate_macd(result_df['close'])
            result_df['macd'] = macd_data['macd']
            result_df['macd_signal'] = macd_data['signal']
            result_df['macd_hist'] = macd_data['histogram']
            
            # Bollinger Bands
            bb_data = TechnicalIndicators.calculate_bollinger_bands(result_df['close'])
            result_df['bollinger_upper'] = bb_data['upper']
            result_df['bollinger_middle'] = bb_data['middle']
            result_df['bollinger_lower'] = bb_data['lower']
            
            # ATR
            result_df['atr'] = TechnicalIndicators.calculate_atr(
                result_df['high'], result_df['low'], result_df['close'], 14
            )
            
            # OBV
            if 'volume' in result_df.columns:
                result_df['obv'] = TechnicalIndicators.calculate_obv(result_df['close'], result_df['volume'])
            
            # VWAP
            if 'volume' in result_df.columns:
                result_df['vwap'] = TechnicalIndicators.calculate_vwap(
                    result_df['high'], result_df['low'], result_df['close'], result_df['volume']
                )
            
            # Stochastic
            stoch_data = TechnicalIndicators.calculate_stochastic(
                result_df['high'], result_df['low'], result_df['close']
            )
            result_df['stoch_k'] = stoch_data['k']
            result_df['stoch_d'] = stoch_data['d']
            
            # ADX
            adx_data = TechnicalIndicators.calculate_adx(
                result_df['high'], result_df['low'], result_df['close']
            )
            result_df['adx'] = adx_data['adx']
            result_df['plus_di'] = adx_data['plus_di']
            result_df['minus_di'] = adx_data['minus_di']
            
            # CCI
            result_df['cci'] = TechnicalIndicators.calculate_cci(
                result_df['high'], result_df['low'], result_df['close']
            )
            
            logger.info("Successfully calculated all technical indicators")
            
        except Exception as e:
            logger.error(f"Error calculating technical indicators: {e}")
        
        return result_df
    
    @staticmethod
    def get_trading_signals(df: pd.DataFrame) -> dict:
        """Generate trading signals based on technical indicators.
        
        Returns a dictionary with signal recommendations.
        """
        signals = {
            "trend": "neutral",
            "momentum": "neutral",
            "volatility": "neutral",
            "overall": "neutral"
        }
        
        try:
            latest = df.iloc[-1]
            
            # Trend signals (based on moving averages)
            if latest.get('sma_20') and latest.get('sma_50'):
                if latest['close'] > latest['sma_20'] > latest['sma_50']:
                    signals["trend"] = "bullish"
                elif latest['close'] < latest['sma_20'] < latest['sma_50']:
                    signals["trend"] = "bearish"
            
            # Momentum signals (based on RSI and MACD)
            if latest.get('rsi'):
                if latest['rsi'] < 30:
                    signals["momentum"] = "oversold"
                elif latest['rsi'] > 70:
                    signals["momentum"] = "overbought"
            
            if latest.get('macd') and latest.get('macd_signal'):
                if latest['macd'] > latest['macd_signal']:
                    signals["momentum"] = "bullish" if signals["momentum"] == "neutral" else signals["momentum"]
                else:
                    signals["momentum"] = "bearish" if signals["momentum"] == "neutral" else signals["momentum"]
            
            # Volatility signals (based on Bollinger Bands)
            if latest.get('bollinger_upper') and latest.get('bollinger_lower'):
                if latest['close'] > latest['bollinger_upper']:
                    signals["volatility"] = "high"
                elif latest['close'] < latest['bollinger_lower']:
                    signals["volatility"] = "low"
            
            # Overall signal
            bullish_count = sum(1 for s in [signals["trend"], signals["momentum"]] if s in ["bullish", "oversold"])
            bearish_count = sum(1 for s in [signals["trend"], signals["momentum"]] if s in ["bearish", "overbought"])
            
            if bullish_count > bearish_count:
                signals["overall"] = "buy"
            elif bearish_count > bullish_count:
                signals["overall"] = "sell"
        
        except Exception as e:
            logger.error(f"Error generating trading signals: {e}")
        
        return signals
