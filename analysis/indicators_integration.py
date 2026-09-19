# analysis/indicators_integration.py  (v4.6 — penalty_extreme removed)
"""
Data Enrichment & Decision Engine v4.6
════════════════════════════════════════════════════════════════════════════
CHANGES FROM v4.5
──────────────────
- REMOVED `DecisionMatrix.penalty_extreme` field.  The old field was
  validated (must equal 0.0) but never used — EXTREME risk is a hard
  block that returns confidence=0.0 immediately.  Removing the field
  eliminates a stale configuration knob.

CHANGES FROM v4.4
──────────────────
- FIXED: `required_warmup` no longer relies on an unused `cfg_hash`
  argument with `@lru_cache`.  The warmup is now cached against a
  stable hash of the config's warmup-relevant integer fields.

CHANGES FROM v4.3
──────────────────
- ADDED MA20/MA50 columns in enrich_market_data output.
- ColumnMapper updated to include MA aliases.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any, Callable, Dict, List, Optional, Tuple, Union,
    Final,
)

import numpy as np
import pandas as pd

from analysis.indicators import INDICATOR_CONFIG, calculate_indicators_df
from analysis.risk import assess_risk_batch, assess_risk_details
from analysis.signals import (
    CONFIG as SIGNAL_CONFIG,
    advanced_signal_strategy,
    apply_strategy_to_df,
    get_strategy,
)

logger = logging.getLogger(__name__)


class Config:
    MIN_QUALITY_DEFAULT: Final[float] = 0.4
    MAX_HISTORY_SIZE: Final[int] = 1000
    WEIGHT_TOLERANCE: Final[float] = 1e-6
    DEFAULT_MIN_CANDLES: Final[int] = 30
    CONFIDENCE_PRECISION: Final[int] = 3
    RISK_SCORE_PRECISION: Final[int] = 1
    QUALITY_PRECISION: Final[int] = 3
    INDICATOR_PRECISION: Final[int] = 6
    MAX_SIGNAL_REASONS: Final[int] = 4
    MAX_RISK_REASONS: Final[int] = 3


class Action(str, Enum):
    BUY  = "buy"
    EXIT = "exit"
    HOLD = "hold"
    WAIT = "wait"

    def __str__(self) -> str:
        return self.value


class RiskLevel(str, Enum):
    LOW     = "Low"
    MEDIUM  = "Medium"
    HIGH    = "High"
    EXTREME = "Extreme"
    UNKNOWN = "Unknown"

    def __str__(self) -> str:
        return self.value


_TF_TO_PERIODS: Final[Dict[str, int]] = {
    "1m": 1440, "3m": 480,  "5m": 288,  "15m": 96,
    "30m": 48,  "1h": 24,   "2h": 12,   "4h": 6,
    "6h": 4,    "8h": 3,    "12h": 2,   "1d": 1,
}

_TF_TO_MINUTES: Final[Dict[str, int]] = {
    "1m": 1,   "3m": 3,    "5m": 5,    "15m": 15,
    "30m": 30, "1h": 60,   "2h": 120,  "4h": 240,
    "6h": 360, "8h": 480,  "12h": 720, "1d": 1440,
}

_LONG_SIGNALS: Final[frozenset] = frozenset({"Strong Buy", "Buy Signal"})
_EXIT_SIGNALS: Final[frozenset] = frozenset({"Sell Signal", "Strong Sell"})
_STRONG_SIGNALS: Final[frozenset] = frozenset({"Strong Buy", "Strong Sell"})
_ACCEPTABLE_RISK: Final[frozenset] = frozenset({RiskLevel.LOW, RiskLevel.MEDIUM})
_BLOCKING_RISK: Final[frozenset] = frozenset({RiskLevel.EXTREME})

_DEFAULT_RISK_INFO: Final[Dict[str, Any]] = {
    "level":       RiskLevel.MEDIUM.value,
    "score":       50.0,
    "reasons":     [],
    "Risk_Score":  5.0,
    "Risk_Level":  RiskLevel.MEDIUM.value,
    "DataQuality": 0.0,
}

# Config keys that influence the warmup period — used to build the cache key
_WARMUP_KEYS: Final[Tuple[str, ...]] = (
    "rsi_period", "macd_slow", "macd_signal",
    "bb_period", "stoch_k", "stoch_d",
    "adx_period", "atr_period", "ema_slow",
    "volume_ma", "cci_period", "willr_period", "ichi_senkou",
)


class ColumnMapper:
    PRICE_COLUMNS: Final[Tuple[str, ...]] = ("close", "Close", "Price")
    VOLUME_COLUMNS: Final[Tuple[str, ...]] = ("volume", "Volume")

    OHLCV_ALIASES: Final[Dict[str, str]] = {
        "close":  "Price",
        "Close":  "Price",
        "open":   "Open",
        "high":   "High",
        "low":    "Low",
        "volume": "Volume",
    }

    MA_ALIASES: Final[Dict[str, str]] = {
        "MA20": "MA20", "ma20": "MA20",
        "MA50": "MA50", "ma50": "MA50",
    }

    EXPECTED_SIGNAL_COLUMNS: Final[Tuple[str, ...]] = (
        "signal", "Signal",
        "score", "Score",
        "confidence", "Confidence",
        "risk", "Risk", "risk_level", "Risk_Level",
        "action", "Action",
        "reasons", "Reasons",
        "quality", "Quality",
        "long_score", "Long_Score",
        "tradable_long", "Tradable_Long",
        "position_size_pct", "Position_Size",
    )

    @staticmethod
    def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
        result = df
        for src, dst in ColumnMapper.OHLCV_ALIASES.items():
            if src in df.columns and dst not in df.columns:
                result[dst] = df[src]
        for src, dst in ColumnMapper.MA_ALIASES.items():
            if src in df.columns and dst not in df.columns:
                result[dst] = df[src]
        return result

    @staticmethod
    def find_column(df: pd.DataFrame, candidates: Tuple[str, ...]) -> Optional[str]:
        return next((c for c in candidates if c in df.columns), None)

    @staticmethod
    def validate_signal_output(result: pd.DataFrame) -> List[str]:
        warnings = []
        if not any(c in result.columns for c in ("signal", "Signal")):
            warnings.append("Missing 'signal' or 'Signal' column")
        if "score" not in result.columns and "Score" not in result.columns:
            warnings.append("Missing 'score' column")
        if not any(c in result.columns for c in ("risk", "Risk", "risk_level", "Risk_Level")):
            warnings.append("Missing risk column")
        return warnings

    @staticmethod
    def ensure_dual_variants(df: pd.DataFrame) -> pd.DataFrame:
        if "Signal" not in df.columns and "signal" in df.columns:
            df["Signal"] = df["signal"]
        elif "signal" not in df.columns and "Signal" in df.columns:
            df["signal"] = df["Signal"]

        if "risk_level" not in df.columns and "Risk_Level" in df.columns:
            df["risk_level"] = df["Risk_Level"]
        elif "Risk_Level" not in df.columns and "risk_level" in df.columns:
            df["Risk_Level"] = df["risk_level"]
        elif "risk_level" not in df.columns and "risk" in df.columns:
            df["risk_level"] = df["risk"]

        if "risk_score" not in df.columns and "Risk_Score" in df.columns:
            df["risk_score"] = df["Risk_Score"]
        elif "Risk_Score" not in df.columns and "risk_score" in df.columns:
            df["Risk_Score"] = df["risk_score"]

        return df


@dataclass
class PositionContext:
    holding:     bool = False
    entry_price: Optional[float] = None
    quantity:    float = 0.0
    entry_time:  Optional[float] = None

    def __post_init__(self) -> None:
        if self.holding and (self.entry_price is None or self.entry_price <= 0):
            raise ValueError("Holding position must have valid entry_price > 0")
        if self.quantity < 0:
            raise ValueError("Quantity cannot be negative")

    def unrealized_pnl_pct(self, current_price: float) -> Optional[float]:
        if not self.holding or not self.entry_price or self.entry_price <= 0:
            return None
        if current_price <= 0:
            return None
        return (current_price - self.entry_price) / self.entry_price * 100.0


@dataclass
class ConfidenceBreakdown:
    base:         float = 0.0
    signal_part:  float = 0.0
    quality_part: float = 0.0
    risk_penalty: float = 0.0
    final:        float = 0.0

    def to_dict(self) -> Dict[str, float]:
        p = Config.CONFIDENCE_PRECISION
        return {
            "base":         round(self.base, p),
            "signal_part":  round(self.signal_part, p),
            "quality_part": round(self.quality_part, p),
            "risk_penalty": round(self.risk_penalty, p),
            "final":        round(self.final, p),
        }


@dataclass
class EnrichmentResult:
    df:              pd.DataFrame
    symbol:          str
    timeframe:       str
    strategy:        str
    initial_rows:    int
    final_rows:      int
    warmup_dropped:  int
    has_signals:     bool
    has_risk:        bool
    elapsed_s:       float
    warnings:        List[str] = field(default_factory=list)

    @property
    def enrichment_ratio(self) -> float:
        return self.final_rows / self.initial_rows if self.initial_rows > 0 else 0.0

    @property
    def is_valid(self) -> bool:
        return (
            not self.df.empty
            and self.final_rows > 0
            and self.has_signals
            and self.has_risk
        )

    def to_summary(self) -> Dict[str, Any]:
        return {
            "symbol":           self.symbol,
            "timeframe":        self.timeframe,
            "strategy":         self.strategy,
            "initial_rows":     self.initial_rows,
            "final_rows":       self.final_rows,
            "warmup_dropped":   self.warmup_dropped,
            "enrichment_ratio": round(self.enrichment_ratio, 3),
            "has_signals":      self.has_signals,
            "has_risk":         self.has_risk,
            "elapsed_s":        round(self.elapsed_s, 3),
            "is_valid":         self.is_valid,
            "warning_count":    len(self.warnings),
        }


@dataclass
class DecisionMatrix:
    base_strong_buy:  float = 0.85
    base_buy:         float = 0.65
    base_exit_held:   float = 0.80
    base_exit_strong: float = 0.92

    weight_base:    float = 0.60
    weight_signal:  float = 0.25
    weight_quality: float = 0.15

    penalty_high:    float = 0.75
    # v4.6: `penalty_extreme` field removed.
    #   Previous versions carried a `penalty_extreme: float = 0.0` field
    #   that was validated to be 0.0 but never actually used in
    #   `compute_entry_confidence()` — EXTREME risk is a hard block that
    #   returns immediately with confidence=0.0.

    min_confidence_high_risk: float = 0.70

    def __post_init__(self) -> None:
        errors = self.validate()
        if errors:
            error_msg = "; ".join(errors)
            logger.error("DecisionMatrix validation failed: %s", error_msg)
            raise ValueError(f"Invalid DecisionMatrix: {error_msg}")

    def validate(self) -> List[str]:
        errors: List[str] = []
        w_sum = self.weight_base + self.weight_signal + self.weight_quality
        if abs(w_sum - 1.0) > Config.WEIGHT_TOLERANCE:
            errors.append(f"Weights sum to {w_sum:.4f}, must be 1.0")
        confidence_attrs = {
            "base_strong_buy": self.base_strong_buy,
            "base_buy": self.base_buy,
            "base_exit_held": self.base_exit_held,
            "base_exit_strong": self.base_exit_strong,
        }
        for name, value in confidence_attrs.items():
            if not 0.0 <= value <= 1.0:
                errors.append(f"{name}={value} outside [0,1]")
        if not 0.0 <= self.penalty_high <= 1.0:
            errors.append(f"penalty_high={self.penalty_high} outside [0,1]")
        return errors

    def compute_entry_confidence(
        self,
        is_strong:      bool,
        sig_confidence: float,
        quality:        float,
        risk_level:     str,
    ) -> Tuple[float, ConfidenceBreakdown]:
        bd = ConfidenceBreakdown()
        try:
            rl = RiskLevel(risk_level)
        except ValueError:
            rl = RiskLevel.UNKNOWN

        if rl == RiskLevel.EXTREME:
            # EXTREME risk is a hard block — no entry regardless of signal.
            logger.debug("Entry blocked: EXTREME risk level")
            bd.final = 0.0
            return 0.0, bd

        if rl == RiskLevel.HIGH:
            if not is_strong:
                logger.debug("Entry blocked: HIGH risk requires strong signal")
                return 0.0, bd
            if sig_confidence < self.min_confidence_high_risk:
                logger.debug(
                    "Entry blocked: HIGH risk requires sig_confidence >= %.2f, got %.2f",
                    self.min_confidence_high_risk, sig_confidence,
                )
                return 0.0, bd

        base = self.base_strong_buy if is_strong else self.base_buy
        bd.base = base
        bd.signal_part = sig_confidence * self.weight_signal
        bd.quality_part = quality * self.weight_quality

        blended = (
            base * self.weight_base
            + bd.signal_part
            + bd.quality_part
        )

        if rl == RiskLevel.HIGH:
            bd.risk_penalty = 1.0 - self.penalty_high
            blended *= self.penalty_high

        bd.final = float(np.clip(blended, 0.0, 1.0))
        return bd.final, bd

    def compute_exit_confidence(
        self,
        is_strong:      bool,
        sig_confidence: float,
    ) -> Tuple[float, ConfidenceBreakdown]:
        bd = ConfidenceBreakdown()
        base = self.base_exit_strong if is_strong else self.base_exit_held
        bd.base = base
        bd.signal_part = sig_confidence * 0.15
        bd.final = float(np.clip(base + bd.signal_part, 0.0, 1.0))
        return bd.final, bd


DEFAULT_MATRIX = DecisionMatrix()


class ValidationError(Exception):
    pass


def validate_dataframe(
    df: Any,
    min_rows: int = 1,
    required_columns: Optional[List[str]] = None,
    caller: str = "",
) -> None:
    prefix = f"{caller}: " if caller else ""
    if df is None:
        raise ValidationError(f"{prefix}DataFrame is None")
    if not isinstance(df, pd.DataFrame):
        raise ValidationError(f"{prefix}Expected DataFrame, got {type(df).__name__}")
    if len(df) < min_rows:
        raise ValidationError(
            f"{prefix}DataFrame has {len(df)} rows, minimum {min_rows} required"
        )
    if required_columns:
        missing = set(required_columns) - set(df.columns)
        if missing:
            raise ValidationError(f"{prefix}Missing required columns: {missing}")


def validate_market_data(data: Any, caller: str = "") -> None:
    prefix = f"{caller}: " if caller else ""
    if data is None:
        raise ValidationError(f"{prefix}Market data is None")
    if not isinstance(data, dict):
        raise ValidationError(f"{prefix}Expected dict, got {type(data).__name__}")
    if not data:
        raise ValidationError(f"{prefix}Market data is empty")


def _warmup_cache_key() -> str:
    """Build a stable cache key from the config values that affect warmup."""
    fields = {k: INDICATOR_CONFIG.get(k) for k in _WARMUP_KEYS}
    raw = json.dumps(fields, sort_keys=True, default=str)
    return hashlib.md5(raw.encode()).hexdigest()


_WARMUP_MEMO: Dict[str, int] = {}


def required_warmup(cfg_hash: Optional[str] = None) -> int:
    """
    Compute the minimum number of candles needed so that every indicator
    has at least one valid (non-NaN) value at the end of the window.

    v4.5: the old `@lru_cache` keyed on an unused `cfg_hash` argument was
    removed because it could not detect runtime changes to INDICATOR_CONFIG.
    We now memoise on a hash of the warmup-relevant config fields, so a
    changed config (e.g. ema_slow 200 → 100) automatically invalidates the
    cache.

    Parameters
    ----------
    cfg_hash : str, optional
        Precomputed cache key.  If None (the normal case), the key is
        derived from the current INDICATOR_CONFIG.
    """
    key = cfg_hash or _warmup_cache_key()
    cached = _WARMUP_MEMO.get(key)
    if cached is not None:
        return cached

    cfg = INDICATOR_CONFIG

    def safe_int(k: str, default: int) -> int:
        try:
            return max(1, int(cfg.get(k, default)))
        except (TypeError, ValueError):
            logger.warning("Invalid config value for %s, using default %d", k, default)
            return default

    requirements = [
        safe_int("rsi_period", 14) + 1,
        safe_int("macd_slow", 26) + safe_int("macd_signal", 9),
        safe_int("bb_period", 20),
        safe_int("stoch_k", 14) + safe_int("stoch_d", 3),
        safe_int("adx_period", 14) * 2,
        safe_int("atr_period", 14),
        safe_int("ema_slow", 200),
        safe_int("volume_ma", 20),
        safe_int("cci_period", 20),
        safe_int("willr_period", 14),
        safe_int("ichi_senkou", 52),
        50,   # MA50 needs 50 candles
    ]
    warmup = max(requirements)

    # Cap the memo size so a pathological caller churning the config does
    # not grow this dict without bound.
    if len(_WARMUP_MEMO) > 32:
        _WARMUP_MEMO.clear()
    _WARMUP_MEMO[key] = warmup

    logger.debug(
        "Computed warmup: %d bars (EMA_slow=%d)",
        warmup, safe_int("ema_slow", 200),
    )
    return warmup


def add_momentum_columns(
    df: pd.DataFrame,
    timeframe: str,
    inplace: bool = True,
) -> pd.DataFrame:
    result = df if inplace else df.copy()
    tf_minutes = _TF_TO_MINUTES.get(timeframe.lower(), 60)
    periods_24h = _TF_TO_PERIODS.get(timeframe.lower(), 24)
    price_col = ColumnMapper.find_column(df, ColumnMapper.PRICE_COLUMNS)
    volume_col = ColumnMapper.find_column(df, ColumnMapper.VOLUME_COLUMNS)

    if price_col is None:
        logger.warning("No price column found, skipping momentum calculations")
        return result

    if "1h Change (%)" not in result.columns and tf_minutes <= 60:
        periods_1h = max(1, round(60 / tf_minutes))
        result["1h Change (%)"] = result[price_col].pct_change(periods=periods_1h) * 100.0

    if "24h Change (%)" not in result.columns and len(df) > periods_24h:
        result["24h Change (%)"] = result[price_col].pct_change(periods=periods_24h) * 100.0

    if "7d Change (%)" not in result.columns:
        periods_7d = periods_24h * 7
        if len(df) > periods_7d:
            result["7d Change (%)"] = result[price_col].pct_change(periods=periods_7d) * 100.0

    if volume_col and "Volume Change 24h (%)" not in result.columns and len(df) > periods_24h:
        result["Volume Change 24h (%)"] = (
            result[volume_col].pct_change(periods=periods_24h) * 100.0
        )
    return result


def apply_signals(
    df: pd.DataFrame,
    strategy: str,
    tag: str = "",
) -> pd.DataFrame:
    try:
        result = apply_strategy_to_df(
            df,
            strategy=strategy,
            enrich=True,
            return_frame=True,
            parallel=False,
        )
        if not isinstance(result, pd.DataFrame) or result.empty:
            logger.warning("%s Strategy returned empty result", tag)
            if "Signal" not in df.columns:
                df["Signal"] = "Neutral"
            if "signal" not in df.columns:
                df["signal"] = "Neutral"
            return df

        validation_warnings = ColumnMapper.validate_signal_output(result)
        for warn in validation_warnings:
            logger.warning("%s %s", tag, warn)

        result = ColumnMapper.ensure_dual_variants(result)
        for col in ["Signal", "signal"]:
            if col in result.columns:
                result[col] = result[col].fillna("Neutral")
        return result
    except Exception as exc:
        logger.error("%s Signal calculation failed: %s", tag, exc, exc_info=True)
        df["Signal"] = "Neutral"
        df["signal"] = "Neutral"
        return df


def apply_risk(df: pd.DataFrame, tag: str = "") -> pd.DataFrame:
    try:
        result = assess_risk_batch(df)
        ColumnMapper.ensure_dual_variants(result)
        if "Risk_Level" in result.columns and "risk" not in result.columns:
            result["risk"] = result["Risk_Level"]
        elif "risk_level" in result.columns and "risk" not in result.columns:
            result["risk"] = result["risk_level"]
        return result
    except Exception as exc:
        logger.error("%s Risk assessment failed: %s", tag, exc, exc_info=True)
        df["Risk_Level"] = RiskLevel.UNKNOWN.value
        df["Risk_Score"] = 5.0
        df["risk_level"] = RiskLevel.UNKNOWN.value
        df["risk_score"] = 5.0
        df["risk"] = RiskLevel.UNKNOWN.value
        return df


def enrich_market_data(
    df: pd.DataFrame,
    calculate_signals: bool = True,
    calculate_risk:    bool = True,
    strategy:          str  = "composite",
    min_candles:       int  = Config.DEFAULT_MIN_CANDLES,
    timeframe:         str  = "1h",
    symbol:            str  = "UNKNOWN",
    trim_warmup:       bool = True,
    cfg:               Optional[Dict[str, Any]] = None,
    return_result_obj: bool = False,
) -> Union[pd.DataFrame, EnrichmentResult]:
    t_start = time.perf_counter()
    warnings_list: List[str] = []
    tag = f"[{symbol}|{timeframe}|{strategy}]"

    try:
        validate_dataframe(df, min_rows=1, caller="enrich_market_data")
    except ValidationError as e:
        logger.warning(str(e))
        if return_result_obj:
            return EnrichmentResult(
                df=pd.DataFrame(), symbol=symbol, timeframe=timeframe,
                strategy=strategy, initial_rows=0, final_rows=0,
                warmup_dropped=0, has_signals=False, has_risk=False,
                elapsed_s=0.0, warnings=[str(e)],
            )
        raise

    initial_len = len(df)
    logger.info("%s Starting enrichment of %d rows", tag, initial_len)

    result = df.copy()
    result = ColumnMapper.normalize_columns(result)
    result = add_momentum_columns(result, timeframe, inplace=True)

    try:
        result = calculate_indicators_df(result, min_rows=min_candles, cfg=cfg)
    except Exception as exc:
        msg = f"Indicator calculation failed: {exc}"
        logger.error("%s %s", tag, msg, exc_info=True)
        warnings_list.append(msg)
        if return_result_obj:
            return EnrichmentResult(
                df=result, symbol=symbol, timeframe=timeframe,
                strategy=strategy, initial_rows=initial_len,
                final_rows=len(result), warmup_dropped=0,
                has_signals=False, has_risk=False,
                elapsed_s=time.perf_counter() - t_start,
                warnings=warnings_list,
            )
        return result

    # ── MA20 / MA50 columns ───────────────────────────────────
    price_col = ColumnMapper.find_column(result, ColumnMapper.PRICE_COLUMNS)
    if price_col:
        if "MA20" not in result.columns:
            result["MA20"] = result[price_col].rolling(window=20).mean()
        if "MA50" not in result.columns:
            result["MA50"] = result[price_col].rolling(window=50).mean()
    else:
        warnings_list.append("No price column found; MA20/MA50 not added.")

    # ── Warmup trim ───────────────────────────────────────────
    warmup_dropped = 0
    if trim_warmup:
        warmup = required_warmup()
        if len(result) > warmup:
            warmup_dropped = warmup
            result = result.iloc[warmup:].reset_index(drop=True)
            logger.info(
                "%s Trimmed warmup: %d → %d rows (dropped %d)",
                tag, initial_len, len(result), warmup_dropped,
            )
        else:
            msg = (
                f"Insufficient data: {len(result)} rows but {warmup} needed. "
                f"Some indicators may have NaN values."
            )
            logger.warning("%s %s", tag, msg)
            warnings_list.append(msg)

    if result.empty:
        logger.warning("%s DataFrame empty after enrichment", tag)
        if return_result_obj:
            return EnrichmentResult(
                df=result, symbol=symbol, timeframe=timeframe,
                strategy=strategy, initial_rows=initial_len,
                final_rows=0, warmup_dropped=warmup_dropped,
                has_signals=False, has_risk=False,
                elapsed_s=time.perf_counter() - t_start,
                warnings=warnings_list,
            )
        return result

    # ── Signals ───────────────────────────────────────────────
    has_signals = False
    if calculate_signals:
        try:
            result = apply_signals(result, strategy, tag)
            has_signals = "Signal" in result.columns or "signal" in result.columns
        except Exception as exc:
            msg = f"Signal application failed: {exc}"
            logger.error("%s %s", tag, msg, exc_info=True)
            warnings_list.append(msg)

    # ── Risk ──────────────────────────────────────────────────
    has_risk = False
    if calculate_risk:
        try:
            result = apply_risk(result, tag)
            has_risk = any(c in result.columns for c in ["Risk_Level", "risk_level", "risk"])
        except Exception as exc:
            msg = f"Risk application failed: {exc}"
            logger.error("%s %s", tag, msg, exc_info=True)
            warnings_list.append(msg)

    elapsed = time.perf_counter() - t_start
    logger.info(
        "%s Enrichment complete: %d rows, %.3fs (signals=%s, risk=%s)",
        tag, len(result), elapsed, has_signals, has_risk,
    )

    if return_result_obj:
        return EnrichmentResult(
            df=result, symbol=symbol, timeframe=timeframe,
            strategy=strategy, initial_rows=initial_len,
            final_rows=len(result), warmup_dropped=warmup_dropped,
            has_signals=has_signals, has_risk=has_risk,
            elapsed_s=elapsed, warnings=warnings_list,
        )
    return result


async def enrich_market_data_async(
    df: pd.DataFrame,
    **kwargs: Any,
) -> Union[pd.DataFrame, EnrichmentResult]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: enrich_market_data(df, **kwargs),
    )


def get_signal_info(
    data: Dict[str, Any],
    strategy: str,
    tag: str = "",
) -> Tuple[str, float, List[str], float, bool]:
    try:
        strategy_fn = get_strategy(strategy) or advanced_signal_strategy
        result = strategy_fn(data)
        signal = str(result.get("signal") or result.get("Signal") or "Neutral")

        raw_conf = result.get("confidence", result.get("Confidence", 50))
        confidence = float(np.clip(float(raw_conf) / 100.0, 0.0, 1.0))

        quality = float(np.clip(
            float(result.get("quality", result.get("Quality", 1.0))),
            0.0, 1.0,
        ))
        tradable = bool(result.get(
            "tradable_long",
            result.get("Tradable_Long", False),
        ))

        raw_reasons = result.get("reasons", result.get("Reasons", ""))
        if isinstance(raw_reasons, str):
            reasons = [r.strip() for r in raw_reasons.split(";") if r.strip()]
        elif isinstance(raw_reasons, list):
            reasons = [str(r) for r in raw_reasons if r]
        else:
            reasons = []

        return signal, confidence, reasons, quality, tradable

    except Exception as exc:
        logger.error("%s Signal extraction error: %s", tag, exc, exc_info=True)
        return "Neutral", 0.0, [f"Error: {exc}"], 0.0, False


def get_risk_info(data: Dict[str, Any], tag: str = "") -> Dict[str, Any]:
    try:
        risk_info = assess_risk_details(data)
        if "level" in risk_info:
            risk_info["risk_level"] = risk_info["level"]
            risk_info["Risk_Level"] = risk_info["level"]
            risk_info["risk"] = risk_info["level"]
        return risk_info
    except Exception as exc:
        logger.error("%s Risk extraction error: %s", tag, exc, exc_info=True)
        return dict(_DEFAULT_RISK_INFO)


def decide_action(
    signal:         str,
    sig_confidence: float,
    risk_level:     str,
    quality:        float,
    tradable_long:  bool,
    holding:        bool,
    matrix:         DecisionMatrix,
) -> Tuple[str, float, ConfidenceBreakdown]:
    is_long_signal = signal in _LONG_SIGNALS
    is_exit_signal = signal in _EXIT_SIGNALS
    is_strong = signal in _STRONG_SIGNALS

    if is_exit_signal:
        if holding:
            conf, bd = matrix.compute_exit_confidence(is_strong, sig_confidence)
            return Action.EXIT.value, conf, bd
        return Action.HOLD.value, 0.0, ConfidenceBreakdown()

    if not is_long_signal:
        return Action.HOLD.value, 0.0, ConfidenceBreakdown()

    if not tradable_long:
        logger.debug(
            "Long signal '%s' not tradable (quality=%.2f)",
            signal, quality,
        )
        return Action.HOLD.value, 0.0, ConfidenceBreakdown()

    conf, bd = matrix.compute_entry_confidence(
        is_strong=is_strong,
        sig_confidence=sig_confidence,
        quality=quality,
        risk_level=risk_level,
    )
    if conf <= 0.0:
        return Action.HOLD.value, 0.0, bd
    return Action.BUY.value, conf, bd


def build_decision_reasons(
    signal:      str,
    risk_info:   Dict[str, Any],
    action:      str,
    sig_reasons: List[str],
    quality:     float,
    tradable:    bool,
    holding:     bool,
) -> List[str]:
    reasons: List[str] = [f"Signal: {signal}"]
    reasons.extend(sig_reasons[:Config.MAX_SIGNAL_REASONS])

    risk_reasons = risk_info.get("reasons", [])
    if isinstance(risk_reasons, list):
        reasons.extend(
            f"Risk: {r}" for r in risk_reasons[:Config.MAX_RISK_REASONS]
        )
    elif isinstance(risk_reasons, str) and risk_reasons:
        reasons.append(f"Risk: {risk_reasons}")

    if action == Action.HOLD.value:
        level = str(risk_info.get("level", "") or risk_info.get("risk_level", ""))
        if level == RiskLevel.EXTREME.value:
            reasons.append("⛔ BLOCKED: Extreme risk — no entry allowed")
        elif signal in _LONG_SIGNALS and not tradable:
            min_q = SIGNAL_CONFIG.get("min_quality_for_long", Config.MIN_QUALITY_DEFAULT)
            reasons.append(
                f"⛔ BLOCKED: Quality {quality:.2f} below threshold {min_q:.2f}"
            )
        elif signal in _EXIT_SIGNALS and not holding:
            reasons.append("ℹ️  Exit signal ignored (no position)")
    return reasons


def extract_indicators(data: Dict[str, Any]) -> Dict[str, Any]:
    def safe_get(*keys: str) -> Optional[float]:
        for key in keys:
            value = data.get(key)
            if value is None:
                continue
            try:
                num = float(value)
                if np.isfinite(num):
                    return round(num, Config.INDICATOR_PRECISION)
            except (TypeError, ValueError):
                continue
        return None

    return {
        "RSI":          safe_get("RSI", "rsi"),
        "MACD":         safe_get("MACD", "macd"),
        "MACD_Signal":  safe_get("MACD Signal", "MACD_Signal", "macd_signal"),
        "MACD_Hist":    safe_get("MACD_Histogram", "macd_hist"),
        "ADX":          safe_get("ADX", "adx"),
        "Plus_DI":      safe_get("+DI", "plus_di"),
        "Minus_DI":     safe_get("-DI", "minus_di"),
        "ATR_pct":      safe_get("ATR %", "ATR_pct", "atr_pct"),
        "BB_Upper":     safe_get("BB Upper", "bb_upper"),
        "BB_Lower":     safe_get("BB Lower", "bb_lower"),
        "BB_Width_pct": safe_get("BB Width %", "bb_width_pct"),
        "BB_PercentB":  safe_get("BB_PercentB", "bb_percent_b"),
        "EMA50":        safe_get("EMA50", "ema50"),
        "EMA200":       safe_get("EMA200", "ema200"),
        "MA20":         safe_get("MA20", "ma20"),
        "MA50":         safe_get("MA50", "ma50"),
        "Stoch_K":      safe_get("Stochastic_K", "stoch_k"),
        "Stoch_D":      safe_get("Stochastic_D", "stoch_d"),
        "CCI":          safe_get("CCI", "cci"),
        "Williams_R":   safe_get("Williams_%R", "williams_r"),
        "Volume_Ratio": safe_get("Relative_Volume", "relative_volume"),
        "VWAP":         safe_get("VWAP", "vwap"),
        "Price":        safe_get("Price", "close", "Close"),
        "Change_1h":    safe_get("1h Change (%)"),
        "Change_24h":   safe_get("24h Change (%)"),
        "Market_Cap":   safe_get("Market Cap", "market_cap"),
    }


def create_error_decision(
    message:  str,
    symbol:   str = "UNKNOWN",
    strategy: str = "unknown",
) -> Dict[str, Any]:
    return {
        "action":               Action.HOLD.value,
        "confidence":           0.0,
        "signal":               "Neutral",
        "Signal":               "Neutral",
        "risk":                 RiskLevel.UNKNOWN.value,
        "risk_level":           RiskLevel.UNKNOWN.value,
        "Risk_Level":           RiskLevel.UNKNOWN.value,
        "risk_score":           0.0,
        "quality":              0.0,
        "tradable_long":        False,
        "reasons":              [f"❌ {message}"],
        "indicators":           {},
        "symbol":               symbol,
        "strategy":             strategy,
        "confidence_breakdown": ConfidenceBreakdown().to_dict(),
    }


def get_trading_decision(
    market_data: Dict[str, Any],
    strategy:    str = "composite",
    symbol:      str = "UNKNOWN",
    holding:     bool = False,
    position:    Optional[PositionContext] = None,
    matrix:      DecisionMatrix = DEFAULT_MATRIX,
) -> Dict[str, Any]:
    tag = f"[{symbol}|{strategy}]"

    try:
        validate_market_data(market_data, caller="get_trading_decision")
    except ValidationError as e:
        logger.warning(str(e))
        return create_error_decision(str(e), symbol, strategy)

    effective_holding = position.holding if position else holding

    signal, sig_conf, sig_reasons, quality, tradable = get_signal_info(
        market_data, strategy, tag,
    )
    risk_info = get_risk_info(market_data, tag)

    risk_level = str(
        risk_info.get("level")
        or risk_info.get("risk_level")
        or risk_info.get("Risk_Level")
        or risk_info.get("risk")
        or RiskLevel.MEDIUM.value
    )
    risk_score_100 = float(
        risk_info.get("score")
        or risk_info.get("Risk_Score")
        or 50.0
    )

    action, confidence, breakdown = decide_action(
        signal=signal,
        sig_confidence=sig_conf,
        risk_level=risk_level,
        quality=quality,
        tradable_long=tradable,
        holding=effective_holding,
        matrix=matrix,
    )

    reasons = build_decision_reasons(
        signal=signal,
        risk_info=risk_info,
        action=action,
        sig_reasons=sig_reasons,
        quality=quality,
        tradable=tradable,
        holding=effective_holding,
    )

    result = {
        "action":               action,
        "confidence":           round(confidence, Config.CONFIDENCE_PRECISION),
        "signal":               signal,
        "Signal":               signal,
        "risk":                 risk_level,
        "risk_level":           risk_level,
        "Risk_Level":           risk_level,
        "risk_score":           round(risk_score_100, Config.RISK_SCORE_PRECISION),
        "quality":              round(quality, Config.QUALITY_PRECISION),
        "tradable_long":        tradable,
        "reasons":              reasons,
        "indicators":           extract_indicators(market_data),
        "symbol":               symbol,
        "strategy":             strategy,
        "confidence_breakdown": breakdown.to_dict(),
    }

    logger.info(
        "%s Decision: %s | conf=%.0f%% | sig=%s | risk=%s(%.0f) | qual=%.2f",
        tag, action.upper(), confidence * 100, signal,
        risk_level, risk_score_100, quality,
    )
    return result


def get_trading_decision_batch(
    symbols_data: Dict[str, Dict[str, Any]],
    strategy:     str = "composite",
    positions:    Optional[Dict[str, PositionContext]] = None,
    matrix:       DecisionMatrix = DEFAULT_MATRIX,
    sort_by:      str = "confidence",
    top_n:        Optional[int] = None,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    positions = positions or {}

    for symbol, data in symbols_data.items():
        pos = positions.get(symbol)
        try:
            decision = get_trading_decision(
                market_data=data,
                strategy=strategy,
                symbol=symbol,
                position=pos,
                matrix=matrix,
            )
            results.append(decision)
        except Exception as exc:
            logger.error(
                "Batch decision failed for %s: %s", symbol, exc, exc_info=True,
            )
            results.append(create_error_decision(str(exc), symbol, strategy))

    valid_sort_keys = {"confidence", "risk_score", "quality"}
    sort_key = sort_by if sort_by in valid_sort_keys else "confidence"
    results.sort(
        key=lambda d: float(d.get(sort_key, 0.0)),
        reverse=True,
    )

    if top_n is not None and top_n > 0:
        results = results[:top_n]

    logger.info(
        "Batch decision: %d symbols → %d results (sorted by %s)",
        len(symbols_data), len(results), sort_key,
    )
    return results


class EnrichmentMetrics:
    __slots__ = ('total_calls', 'total_rows', 'total_elapsed', 'errors', '_history')

    def __init__(self) -> None:
        self.total_calls:   int = 0
        self.total_rows:    int = 0
        self.total_elapsed: float = 0.0
        self.errors:        int = 0
        self._history:      List[EnrichmentResult] = []

    def record(self, result: EnrichmentResult) -> None:
        self.total_calls += 1
        self.total_rows += result.final_rows
        self.total_elapsed += result.elapsed_s
        self.errors += len(result.warnings)
        self._history.append(result)
        if len(self._history) > Config.MAX_HISTORY_SIZE:
            self._history.pop(0)

    @property
    def avg_elapsed(self) -> float:
        return self.total_elapsed / self.total_calls if self.total_calls > 0 else 0.0

    @property
    def avg_rows(self) -> float:
        return self.total_rows / self.total_calls if self.total_calls > 0 else 0.0

    @property
    def error_rate(self) -> float:
        return self.errors / self.total_calls if self.total_calls > 0 else 0.0

    def summary(self) -> Dict[str, Any]:
        return {
            "total_calls":   self.total_calls,
            "total_rows":    self.total_rows,
            "avg_elapsed_s": round(self.avg_elapsed, 4),
            "avg_rows":      round(self.avg_rows, 1),
            "total_errors":  self.errors,
            "error_rate":    round(self.error_rate, 3),
            "history_size":  len(self._history),
        }

    def reset(self) -> None:
        self.total_calls = 0
        self.total_rows = 0
        self.total_elapsed = 0.0
        self.errors = 0
        self._history.clear()


METRICS = EnrichmentMetrics()


__all__ = [
    "enrich_market_data",
    "enrich_market_data_async",
    "get_trading_decision",
    "get_trading_decision_batch",
    "required_warmup",
    "EnrichmentResult",
    "PositionContext",
    "ConfidenceBreakdown",
    "DecisionMatrix",
    "DEFAULT_MATRIX",
    "EnrichmentMetrics",
    "METRICS",
    "Action",
    "RiskLevel",
    "Config",
    "ColumnMapper",
    "ValidationError",
    "validate_dataframe",
    "validate_market_data",
    "apply_signals",
    "apply_risk",
    "add_momentum_columns",
    "get_signal_info",
    "get_risk_info",
    "decide_action",
    "build_decision_reasons",
    "extract_indicators",
    "create_error_decision",
]