# analysis/risk.py  (v4.4 — monotonic cache + OrderedDict eviction + MA20_Dev fix)
"""
Multi-Engine Crypto Risk Assessment Engine v4.4
────────────────────────────────────────────────────────────────────────────
CHANGES FROM v4.3
─────────────────
1. `RiskCache.get_or_compute` now uses `time.monotonic()` instead of
   `time.time()` for the TTL clock.  This matches the pattern used by
   `core/utils.py::LRUCache` and makes the cache immune to wall-clock
   changes (NTP adjustments, DST, manual clock changes).
2. No functional change to risk computation itself.

CHANGES FROM v4.2 (retained)
────────────────────────────
- `_build_engine_specs` cached with lru_cache(maxsize=1).
- `RiskCache` uses `collections.OrderedDict` for O(1) LRU eviction.
- FIXED: `_derive_features` MA20_Dev fallback.
- MA20/MA50 factors added to 'trend' engine.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache
from typing import (
    Any, Callable, Dict, Iterator, List,
    Optional, Tuple, Union
)

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════
# CONFIGURATION
# ════════════════════════════════════════════════════════════
CONFIG: Dict[str, Any] = {
    "spot_mode": True,
    "engine_weights_base": {
        "liquidity":   1.30,
        "market":      1.10,
        "volatility":  1.30,
        "trend":       1.00,
        "technical":   0.90,
        "volume":      0.90,
        "correlation": 0.60,
        "orderbook":   0.90,
        "derivatives": 0.40,
        "onchain":     0.70,
        "macro":       0.40,
    },
    "engine_regime_sensitivity": {
        "liquidity":   0.35,
        "market":      0.15,
        "volatility":  0.25,
        "trend":       0.20,
        "technical":   0.10,
        "volume":      0.15,
        "correlation": 0.30,
        "orderbook":   0.30,
        "derivatives": 0.20,
        "onchain":     0.20,
        "macro":       0.10,
    },
    "z_score_clip":     3.0,
    "z_to_risk_center": 5.0,
    "z_to_risk_span":   5.0,
    "risk_floor":       0.0,
    "risk_cap":        10.0,
    "avail_tiers": [
        (0.70, 1.00),
        (0.35, 0.95),
        (0.15, 0.70),
        (0.00, 0.30),
    ],
    "batch_use_threads": False,
    "batch_max_workers": 4,
    "batch_timeout_sec": 30,
    "batch_error_policy": "neutral",
    "risk_thresholds": {
        "extreme": 8.0,
        "high":    6.5,
        "medium":  4.5,
    },
    "dd_atr_multiplier":  1.5,
    "dd_default_atr_pct": 4.0,
    "dd_max_pct":        60.0,
    "regime_neutral_fng": 50.0,

    "cache_enabled":  True,
    "cache_ttl_sec":  300,
    "cache_max_size": 10_000,

    "stress_scenarios": {
        "mild_bear":    {"volatility_mult": 1.5, "liquidity_mult": 1.3, "mcap_mult": 0.8},
        "severe_crash": {"volatility_mult": 3.0, "liquidity_mult": 2.5, "mcap_mult": 0.5},
        "liquidity_crisis": {"liquidity_mult": 4.0, "spread_mult": 3.0, "mcap_mult": 0.9},
    },

    "norm_out_of_range_cap": 1.5,
}

# ════════════════════════════════════════════════════════════
# FEATURE METADATA
# ════════════════════════════════════════════════════════════
FEATURE_META_DIRECTION: Dict[str, int] = {
    "MarketCap":             -1,
    "Turnover":              -1,
    "Spread":                +1,
    "FundingRateAbs":        +1,
    "OpenInterestChange":    +1,
    "LongShortImbalance":    +1,
    "Liquidations":          +1,
    "Netflow":               +1,
    "WhaleAccumulation":     +1,
    "DormantCirculation":    +1,
    "SmartMoneyScore":       -1,
    "NVT":                   +1,
    "MVRV":                  +1,
    "Correlation":           +1,
    "MomentumAbs":           +1,
    "ADX":                   +1,
    "MACDAbs":               +1,
    "RSIExtreme":            +1,
    "StochExtreme":          +1,
    "RelativeVolume":        +1,
    "VolumeZ":               +1,
    "OBV_SlopeAbs":          +1,
    "CVDAbs":                +1,
    "OrderFlowImbalanceAbs": +1,
    "BidAskImbalanceAbs":    +1,
    "BookDepth":             -1,
    "ATR":                   +1,
    "HV":                    +1,
    "MacroSurprise":         +1,
    # NEW for MA trend factors
    "MA20_Dev":              +1,
    "MA_Cross":              +1,
}

FEATURE_NORM_RANGES: Dict[str, Tuple[float, float]] = {
    "Market Cap":                 (1e6, 1e12),
    "24h Volume":                 (1e4, 1e11),
    "ATR %":                      (0.5, 20.0),
    "Historical_Volatility_30d":  (0.1, 2.0),
    "7d Change (%)":              (-50.0, 50.0),
    "MACD":                       (-0.05, 0.05),
    "RSI":                        (0.0, 100.0),
    "Volume Change 24h (%)":      (-90.0, 500.0),
    "FundingRate":                (-0.01, 0.01),
    "LongShortRatio":             (0.5, 2.0),
    "Spread %":                   (0.0, 2.0),
    "Fear_Greed_Index":           (0.0, 100.0),
    "Turnover":                   (0.0, 2.0),
    "Netflow":                    (-1000.0, 1000.0),
    "NVT":                        (10.0, 200.0),
    "MVRV":                       (0.5, 5.0),
    "Correlation":                (-1.0, 1.0),
    "BookDepth":                  (0.0, 10.0),
    "RSI_DEV":       (0.0, 1.0),
    "STOCH_DEV":     (0.0, 1.0),
    "MOM_ABS_7D":    (0.0, 40.0),
    "MOM_STRETCH":   (0.0, 4.0),
    "FUNDING_ABS":   (0.0, 0.008),
    "LS_DEV":        (0.0, 1.0),
    "VOL_CHG_ABS":   (0.0, 250.0),
    "CORR_ABS":      (0.0, 1.0),
    "NETFLOW_ABS":   (0.0, 800.0),
    "OI_CHG_ABS":    (0.0, 50.0),
    "LIQ_USD":       (0.0, 1e8),
    "BDEPTH_IMBAL":  (0.0, 1.0),
    # MA-based factor ranges
    "MA20_Dev":      (0.0, 3.0),
    "MA_Cross":      (0.0, 2.0),
}

FEATURE_LOG_SCALE: frozenset = frozenset({
    "Market Cap", "24h Volume", "Volume", "LIQ_USD",
})

_ONE_SIDED_KEYS: frozenset = frozenset({
    "RSI_DEV", "STOCH_DEV", "MOM_ABS_7D", "MOM_STRETCH",
    "FUNDING_ABS", "LS_DEV", "VOL_CHG_ABS", "CORR_ABS",
    "NETFLOW_ABS", "OI_CHG_ABS", "LIQ_USD", "BDEPTH_IMBAL",
    "MA20_Dev", "MA_Cross",
})

_NORM_DEFAULT_RANGE: Tuple[float, float] = (0.0, 100.0)

# ════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════

def is_valid_number(x: Any) -> bool:
    if x is None or isinstance(x, bool):
        return False
    try:
        return bool(np.isfinite(float(x)))
    except (ValueError, TypeError, OverflowError):
        return False


def _safe_get(row: Union[Dict[str, Any], pd.Series], key: str, default: Any = None) -> Any:
    try:
        val = row.get(key, default) if hasattr(row, "get") else row[key]
    except (KeyError, IndexError, TypeError):
        return default
    if val is None:
        return default
    try:
        if np.isscalar(val) and not np.isfinite(float(val)):
            return default
    except (TypeError, ValueError, OverflowError):
        return default
    return val


def _first_valid(row: Union[Dict[str, Any], pd.Series], *keys: str) -> Optional[float]:
    for k in keys:
        v = _safe_get(row, k)
        if is_valid_number(v):
            return float(v)
    return None


def _clip(x: float, lo: float, hi: float) -> float:
    return float(min(max(float(x), lo), hi))


def _normalize_feature(raw: float, feature_key: str) -> float:
    lo, hi = FEATURE_NORM_RANGES.get(feature_key, _NORM_DEFAULT_RANGE)
    value = float(raw)

    if feature_key in FEATURE_LOG_SCALE:
        value = math.log10(max(value, 1.0))
        lo    = math.log10(max(lo, 1.0))
        hi    = math.log10(max(hi, 10.0))

    rng = hi - lo
    if rng == 0:
        return 0.0

    normalized = (value - lo) / rng
    cap = float(CONFIG.get("norm_out_of_range_cap", 1.5))
    normalized = _clip(normalized, -cap, 1.0 + cap)

    if feature_key in _ONE_SIDED_KEYS:
        return _clip(normalized * 3.0, 0.0, 3.0)

    centered = (normalized - 0.5) * 2.0
    return _clip(centered * 3.0, -3.0, 3.0)


def _z_to_risk(z: float) -> float:
    zc = _clip(z, -CONFIG["z_score_clip"], CONFIG["z_score_clip"])
    z_n = zc / CONFIG["z_score_clip"]
    risk = CONFIG["z_to_risk_center"] + z_n * CONFIG["z_to_risk_span"]
    return _clip(risk, CONFIG["risk_floor"], CONFIG["risk_cap"])


def _risk_level(score: float) -> str:
    t = CONFIG["risk_thresholds"]
    if score >= t["extreme"]: return "Extreme"
    if score >= t["high"]: return "High"
    if score >= t["medium"]: return "Medium"
    return "Low"


_TRANSFORMS: Dict[str, Callable[[float], float]] = {
    "abs":               lambda v: abs(v),
    "rsi_dev":           lambda v: abs(v - 50.0) / 50.0,
    "momentum_abs":      lambda v: abs(v),
    "macd_abs":          lambda v: abs(v),
    "dev_from_one_abs":  lambda v: abs(v - 1.0),
    "neg":               lambda v: -v,
    "log1p":             lambda v: math.log1p(max(v, 0.0)),
    "sqrt_abs":          lambda v: math.sqrt(abs(v)),
}


def _apply_transform(val: float, transform: Optional[str]) -> float:
    if not transform:
        return float(val)
    fn = _TRANSFORMS.get(transform)
    if fn is None:
        return float(val)
    return float(fn(float(val)))


def _get_avail_multiplier(availability: float) -> float:
    for threshold, multiplier in CONFIG["avail_tiers"]:
        if availability >= threshold:
            return float(multiplier)
    return 0.30


# ════════════════════════════════════════════════════════════
# DATA CLASSES
# ════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class FactorSpec:
    factor_key: str
    row_key: str
    weight: float
    direction: int
    transform: Optional[str] = None
    norm_key: Optional[str] = None
    alt_keys: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if self.direction not in (+1, -1):
            raise ValueError(f"FactorSpec '{self.factor_key}': direction must be +1 or -1")
        if self.weight < 0:
            raise ValueError(f"FactorSpec '{self.factor_key}': weight must be >= 0")


@dataclass
class EngineResult:
    engine: str
    score: float
    confidence: float
    availability: float
    z_score: float
    contribution: float = 0.0
    factor_risks: Dict[str, float] = field(default_factory=dict)
    dominant_factor: Optional[str] = None
    factor_z_scores: Dict[str, float] = field(default_factory=dict)

    def is_usable(self) -> bool:
        return self.availability > 0.0 and self.confidence > 0.0


@dataclass
class RiskSummary:
    symbol: str = ""
    risk_score: float = 5.0
    risk_level: str = "Medium"
    expected_drawdown: float = 0.0
    confidence: float = 0.0
    data_quality: float = 0.0
    bear_factor: float = 0.5
    engines: Dict[str, float] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    stress_results: Dict[str, float] = field(default_factory=dict)

    def is_high_risk(self) -> bool:
        return self.risk_level in ("High", "Extreme")

    def tradable(self) -> bool:
        return (
            self.risk_level not in ("Extreme",)
            and self.risk_score < 8.0
            and self.engines.get("Liquidity_Risk", 10) < 7.5
        )


# ════════════════════════════════════════════════════════════
# ENGINE SPECIFICATIONS — cached (v4.3)
# ════════════════════════════════════════════════════════════

@lru_cache(maxsize=1)
def _build_engine_specs() -> Dict[str, Tuple[FactorSpec, ...]]:
    """
    Build the engine factor specification table ONCE.

    v4.3: wrapped in lru_cache(maxsize=1) — this dict is immutable by
    convention and rebuilding it for every DataFrame row was wasted work.
    Callers must NOT mutate the returned dict or its tuples.
    """
    D = FEATURE_META_DIRECTION
    return {
        "market": (
            FactorSpec("mcap", "Market Cap", 1.0, D["MarketCap"], norm_key="Market Cap", alt_keys=("market_cap", "MarketCap")),
        ),
        "liquidity": (
            FactorSpec("turnover", "Turnover", 1.2, D["Turnover"], norm_key="Turnover", alt_keys=("Turnover Ratio (%)", "turnover")),
            FactorSpec("spread", "Spread %", 0.9, D["Spread"], norm_key="Spread %", alt_keys=("Spread", "spread_pct")),
            FactorSpec("dollar_vol", "24h Volume", 1.1, -1, norm_key="24h Volume", alt_keys=("Volume", "volume", "quote_volume")),
            FactorSpec("bookdepth", "BookDepth", 0.6, D["BookDepth"], norm_key="BookDepth"),
        ),
        "volatility": (
            FactorSpec("atr", "ATR %", 1.2, D["ATR"], norm_key="ATR %", alt_keys=("ATR_pct", "atr_pct")),
            FactorSpec("hv", "Historical_Volatility_30d", 1.0, D["HV"], norm_key="Historical_Volatility_30d"),
        ),
        "trend": (
            FactorSpec("mom7", "7d Change (%)", 1.0, D["MomentumAbs"], transform="momentum_abs", norm_key="MOM_ABS_7D"),
            FactorSpec("stretch", "Momentum_Stretch", 1.1, +1, transform="abs", norm_key="MOM_STRETCH"),
            FactorSpec("ma20_dev", "MA20_Dev", 0.8, D["MA20_Dev"], transform="abs", norm_key="MA20_Dev"),
            FactorSpec("ma_cross", "MA_Cross", 0.6, D["MA_Cross"], transform="abs", norm_key="MA_Cross"),
        ),
        "technical": (
            FactorSpec("rsi", "RSI", 1.0, D["RSIExtreme"], transform="rsi_dev", norm_key="RSI_DEV", alt_keys=("rsi",)),
            FactorSpec("stoch", "Stochastic_K", 0.7, D["StochExtreme"], transform="rsi_dev", norm_key="STOCH_DEV", alt_keys=("Stochastic", "stoch_k")),
        ),
        "volume": (
            FactorSpec("relvol", "Volume Change 24h (%)", 1.0, D["RelativeVolume"], transform="abs", norm_key="VOL_CHG_ABS"),
        ),
        "derivatives": (
            FactorSpec("funding", "FundingRate", 1.0, D["FundingRateAbs"], transform="abs", norm_key="FUNDING_ABS", alt_keys=("Funding Rate", "funding_rate")),
            FactorSpec("ls_ratio", "LongShortRatio", 0.9, D["LongShortImbalance"], transform="dev_from_one_abs", norm_key="LS_DEV"),
        ),
        "orderbook": (
            FactorSpec("of_imbal", "OrderFlowImbalance", 0.9, D["OrderFlowImbalanceAbs"], transform="abs", norm_key="BDEPTH_IMBAL", alt_keys=("order_flow_imbalance",)),
            FactorSpec("bid_ask_imbal", "BidAskImbalance", 0.8, D["BidAskImbalanceAbs"], transform="abs", norm_key="BDEPTH_IMBAL", alt_keys=("bid_ask_imbalance",)),
        ),
        "correlation": (
            FactorSpec("corr", "Correlation", 1.0, D["Correlation"], transform="abs", norm_key="CORR_ABS", alt_keys=("BTC_Correlation", "btc_correlation")),
        ),
        "onchain": (
            FactorSpec("netflow", "Netflow", 1.0, D["Netflow"], norm_key="Netflow"),
            FactorSpec("nvt", "NVT", 1.0, D["NVT"], norm_key="NVT"),
            FactorSpec("mvrv", "MVRV", 1.0, D["MVRV"], norm_key="MVRV"),
        ),
        "macro": (
            FactorSpec("fear_greed", "Fear_Greed_Index", 1.0, D["MacroSurprise"], transform="rsi_dev", norm_key="Fear_Greed_Index"),
        ),
    }


# ════════════════════════════════════════════════════════════
# DATA QUALITY & FEATURE DERIVATION
# ════════════════════════════════════════════════════════════

_BASIC_FIELDS = ("Market Cap", "24h Volume", "RSI", "Price", "24h Change (%)")
_PREMIUM_FIELDS = ("Spread %", "FundingRate", "LongShortRatio", "Historical_Volatility_30d", "Fear_Greed_Index")
_BASIC_ALTS = {
    "24h Volume": ("Volume", "volume"),
    "Market Cap": ("market_cap", "MarketCap"),
    "RSI":        ("rsi",),
    "Price":      ("close", "Close"),
}


def _compute_data_quality(row: Dict[str, Any]) -> float:
    basic_cnt = 0
    for f in _BASIC_FIELDS:
        keys = (f,) + _BASIC_ALTS.get(f, ())
        if _first_valid(row, *keys) is not None:
            basic_cnt += 1

    prem_cnt = sum(1 for f in _PREMIUM_FIELDS if is_valid_number(_safe_get(row, f)))

    quality = (basic_cnt / len(_BASIC_FIELDS)) * 0.70 + (prem_cnt / len(_PREMIUM_FIELDS)) * 0.30
    return _clip(quality, 0.05, 1.0)


def _derive_features(row: Dict[str, Any]) -> Dict[str, Any]:
    derived: Dict[str, Any] = {}

    if not is_valid_number(_safe_get(row, "Turnover")):
        vol = _first_valid(row, "24h Volume", "Volume", "volume", "quote_volume")
        mcap = _first_valid(row, "Market Cap", "market_cap", "MarketCap")
        if vol is not None and mcap and mcap > 0:
            derived["Turnover"] = vol / mcap

    if not is_valid_number(_safe_get(row, "Momentum_Stretch")):
        chg = _first_valid(row, "24h Change (%)")
        atr = _first_valid(row, "ATR %", "ATR_pct", "atr_pct")
        if atr is None:
            atr_abs = _first_valid(row, "ATR", "atr")
            price = _first_valid(row, "Price", "close", "Close")
            if atr_abs is not None and price and price > 0:
                atr = atr_abs / price * 100.0
        if chg is not None and atr and atr > 0.05:
            derived["Momentum_Stretch"] = chg / atr

    if not is_valid_number(_safe_get(row, "BidAskImbalance")):
        bid_vol = _first_valid(row, "Bid_Volume", "bid_volume")
        ask_vol = _first_valid(row, "Ask_Volume", "ask_volume")
        if bid_vol is not None and ask_vol is not None:
            total = bid_vol + ask_vol
            if total > 0:
                derived["BidAskImbalance"] = abs(bid_vol - ask_vol) / total

    if not is_valid_number(_safe_get(row, "ATR %")):
        atr_abs = _first_valid(row, "ATR", "atr")
        price = _first_valid(row, "Price", "close", "Close")
        if atr_abs is not None and price and price > 0:
            derived["ATR %"] = atr_abs / price * 100.0

    # ── MA-derived factors ────────────────────────────────────
    ma20 = _first_valid(row, "MA20", "ma20")
    ma50 = _first_valid(row, "MA50", "ma50")
    price = _first_valid(row, "Price", "close", "Close")

    if ma20 is not None and price is not None and ma20 > 0:
        atr_pct = _first_valid(row, "ATR %", "ATR_pct", "atr_pct")
        if atr_pct is None:
            atr_abs = _first_valid(row, "ATR", "atr")
            if atr_abs is not None and price > 0:
                atr_pct = atr_abs / price * 100.0

        pct_dev = abs(price - ma20) / ma20 * 100.0
        if atr_pct is not None and atr_pct > 0.05:
            derived["MA20_Dev"] = pct_dev / atr_pct
        else:
            # FIX v4.3: fallback divides by nominal 3% ATR instead of leaving
            # the raw percentage (which would clamp to the 0..3 norm cap).
            derived["MA20_Dev"] = pct_dev / 3.0

    if ma20 is not None and ma50 is not None and ma50 > 0:
        derived["MA_Cross"] = abs(ma20 - ma50) / ma50 * 100.0

    return derived


def _merge_derived(row: Dict[str, Any], derived: Dict[str, Any]) -> Dict[str, Any]:
    if not derived:
        return row
    merged = dict(row)
    merged.update(derived)
    return merged


# ════════════════════════════════════════════════════════════
# CORE ENGINE & FUSION
# ════════════════════════════════════════════════════════════

def _compute_engine(engine: str, row: Dict[str, Any], specs: Tuple[FactorSpec, ...], dq_conf: float) -> EngineResult:
    z_sum = 0.0
    w_sum = 0.0
    avail_cnt = 0
    total = len(specs)
    factor_risks: Dict[str, float] = {}
    factor_z_scores: Dict[str, float] = {}
    max_contrib = 0.0
    dominant_factor: Optional[str] = None

    for spec in specs:
        raw = _first_valid(row, spec.row_key, *spec.alt_keys)
        if raw is None:
            continue

        avail_cnt += 1
        v_t = _apply_transform(raw, spec.transform)
        norm_key = spec.norm_key or spec.row_key
        z_raw = _normalize_feature(v_t, norm_key)
        z_adj = _clip(float(spec.direction) * z_raw, -3.0, 3.0)

        contrib = spec.weight * abs(z_adj)
        if contrib > max_contrib:
            max_contrib = contrib
            dominant_factor = spec.factor_key

        z_sum += spec.weight * z_adj
        w_sum += spec.weight
        factor_risks[spec.factor_key] = _z_to_risk(z_adj)
        factor_z_scores[spec.factor_key] = round(z_adj, 4)

    availability = (avail_cnt / total) if total > 0 else 0.0

    if w_sum <= 0:
        return EngineResult(
            engine=engine, score=5.0, confidence=0.0,
            availability=0.0, z_score=0.0,
            factor_risks=factor_risks, factor_z_scores=factor_z_scores,
        )

    engine_z = z_sum / w_sum
    raw_score = _z_to_risk(engine_z)
    avail_mult = _get_avail_multiplier(availability)
    final_score = _clip(5.0 + (raw_score - 5.0) * avail_mult, 0.0, 10.0)
    confidence = _clip(dq_conf * avail_mult, 0.0, 1.0)

    return EngineResult(
        engine=engine, score=final_score, confidence=confidence,
        availability=availability, z_score=engine_z,
        factor_risks=factor_risks, factor_z_scores=factor_z_scores,
        dominant_factor=dominant_factor,
    )


def _fusion_bayesian(engines: Dict[str, EngineResult], row: Dict[str, Any]) -> Dict[str, Any]:
    fng = _first_valid(row, "Fear_Greed_Index")
    if fng is None:
        fng = float(CONFIG["regime_neutral_fng"])
    fng = _clip(fng, 0.0, 100.0)
    bear_factor = 1.0 - (fng / 100.0)

    num = 0.0
    den = 0.0
    contribs: Dict[str, float] = {}

    for eng, res in engines.items():
        if not res.is_usable():
            continue

        base_w = float(CONFIG["engine_weights_base"].get(eng, 1.0))
        regime_s = float(CONFIG["engine_regime_sensitivity"].get(eng, 0.0))
        adj_w = base_w * (1.0 + regime_s * bear_factor)

        reliab = _clip(res.confidence, 0.0, 1.0) ** 1.5
        w_eff = adj_w * reliab

        if w_eff <= 1e-9:
            continue

        num += res.score * w_eff
        den += w_eff
        contribs[eng] = w_eff

    if den <= 0:
        return {"score": 5.0, "shares": {}, "bear_factor": bear_factor, "den": 0.0}

    fused = _clip(num / den, 0.0, 10.0)
    shares = {k: round(v / den, 4) for k, v in contribs.items()}

    return {"score": fused, "shares": shares, "bear_factor": bear_factor, "den": den}


def _atr_pct_for_dd(row: Dict[str, Any], vol_engine: Optional[EngineResult]) -> float:
    atr_pct = _first_valid(row, "ATR %", "ATR_pct", "atr_pct")
    if atr_pct is not None:
        return _clip(atr_pct, 0.1, 50.0)

    atr_abs = _first_valid(row, "ATR", "atr")
    price = _first_valid(row, "Price", "close", "Close")
    if atr_abs is not None and price and price > 0:
        return _clip(atr_abs / price * 100.0, 0.1, 50.0)

    if vol_engine is not None and vol_engine.is_usable():
        return _clip(1.0 + (vol_engine.score / 10.0) ** 1.6 * 14.0, 0.5, 30.0)

    return float(CONFIG["dd_default_atr_pct"])


def _compute_risk_core(row: Dict[str, Any]) -> Dict[str, Any]:
    derived = _derive_features(row)
    merged_row = _merge_derived(row, derived)

    dq_conf = _compute_data_quality(merged_row)
    engine_specs = _build_engine_specs()

    engines: Dict[str, EngineResult] = {
        eng: _compute_engine(eng, merged_row, specs, dq_conf)
        for eng, specs in engine_specs.items()
    }

    fusion = _fusion_bayesian(engines, merged_row)
    final_score = fusion["score"]

    atr_pct = _atr_pct_for_dd(merged_row, engines.get("volatility"))
    expected_dd = _clip(
        atr_pct * CONFIG["dd_atr_multiplier"] * (1.0 + final_score / 10.0),
        0.0, CONFIG["dd_max_pct"],
    )

    def _eng(name: str) -> float:
        r = engines.get(name)
        return round(r.score, 3) if (r and r.is_usable()) else 5.0

    return {
        "Risk_Score":        round(final_score, 3),
        "Risk_Level":        _risk_level(final_score),
        "Risk_Confidence":   round(dq_conf * 100, 1),
        "Confidence":        round(dq_conf * 100, 1),
        "Expected_Drawdown": round(expected_dd, 2),
        "ATR_pct_used":      round(atr_pct, 3),
        "Liquidity_Risk":    _eng("liquidity"),
        "Volatility_Risk":   _eng("volatility"),
        "Market_Risk":       _eng("market"),
        "Trend_Risk":        _eng("trend"),
        "Technical_Risk":    _eng("technical"),
        "Volume_Risk":       _eng("volume"),
        "Derivatives_Risk":  _eng("derivatives"),
        "Orderbook_Risk":    _eng("orderbook"),
        "Onchain_Risk":      _eng("onchain"),
        "DataQuality":       round(dq_conf * 100, 1),
        "Bear_Factor":       round(fusion.get("bear_factor", 0.5), 3),
        "Engine_Shares":     fusion.get("shares", {}),
        "_engines":          engines,
    }


# ════════════════════════════════════════════════════════════
# RISK CACHE (v4.4 — monotonic clock + OrderedDict eviction)
# ════════════════════════════════════════════════════════════

class RiskCache:
    """
    Thread-safe LRU-style cache for `_compute_risk_core` results.

    v4.4:
      - Switched from `time.time()` to `time.monotonic()` for the TTL
        clock.  This matches the pattern used elsewhere in the project
        (see core/utils.py::LRUCache) and makes the cache immune to
        wall-clock changes such as NTP adjustments or DST.
      - Kept the O(1) LRU eviction via collections.OrderedDict.

    v4.3:
      - Switched from a plain dict + O(n) min-scan to
        `collections.OrderedDict` + `popitem(last=False)` so eviction is
        O(1) even at the 10_000-entry cap.
    """

    def __init__(self, ttl_sec: Optional[int] = None, max_size: Optional[int] = None):
        self._ttl = int(ttl_sec or CONFIG.get("cache_ttl_sec", 300))
        self._max = int(max_size or CONFIG.get("cache_max_size", 10_000))
        self._store: "OrderedDict[str, Tuple[float, Dict[str, Any]]]" = OrderedDict()
        self._lock = threading.Lock()

    def _row_key(self, row: Dict[str, Any]) -> str:
        raw = json.dumps(row, sort_keys=True, default=str)
        return hashlib.md5(raw.encode()).hexdigest()

    def get_or_compute(
        self,
        row: Dict[str, Any],
        compute_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not CONFIG.get("cache_enabled", True):
            return compute_fn(row)

        key = self._row_key(row)
        # v4.4: monotonic clock — immune to wall-clock changes.
        now = time.monotonic()

        with self._lock:
            entry = self._store.get(key)
            if entry is not None:
                ts, cached = entry
                if now - ts < self._ttl:
                    # LRU touch: move to end so it is evicted last
                    self._store.move_to_end(key)
                    return cached
                # Expired — drop it and fall through to recompute
                del self._store[key]

        result = compute_fn(row)

        with self._lock:
            if len(self._store) >= self._max:
                # O(1) LRU eviction
                self._store.popitem(last=False)
            self._store[key] = (now, result)

        return result

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._store)


_global_cache = RiskCache()


# ════════════════════════════════════════════════════════════
# EXPLAINERS & COMPARATORS
# ════════════════════════════════════════════════════════════

class RiskExplainer:
    _LEVEL_EMOJI = {"Extreme": "🚨", "High": "🔴", "Medium": "🟡", "Low": "🟢", "Unknown": "⚪"}
    _ENGINE_MESSAGES: Dict[str, List[Tuple[float, str]]] = {
        "Volatility_Risk": [(8.5, "Very high volatility — stop-loss may trigger easily"), (7.0, "High volatility — size down")],
        "Liquidity_Risk": [(7.5, "Weak liquidity — exit slippage risk"), (6.5, "Liquidity below average")],
        "Market_Risk": [(7.5, "Small market — sensitive to whale flow"), (6.0, "Limited market cap")],
        "Trend_Risk": [(7.0, "Unstable or extended trend")],
        "Technical_Risk": [(7.0, "Technical indicators in an extreme zone")],
        "Volume_Risk": [(7.0, "Unusual volume — possible news or manipulation")],
        "Derivatives_Risk": [(7.5, "Funding / long-short is unbalanced")],
        "Orderbook_Risk": [(7.0, "Order-book imbalance — sudden move risk")],
        "Onchain_Risk": [(7.0, "On-chain signals are cautionary")],
    }

    def explain(self, result: Dict[str, Any], symbol: str = "", verbose: bool = False) -> str:
        level = result.get("Risk_Level", "Unknown")
        score = result.get("Risk_Score", 5.0)
        emoji = self._LEVEL_EMOJI.get(level, "⚪")
        dd = result.get("Expected_Drawdown", 0.0)
        dq = result.get("DataQuality", 0.0)

        header = f"{emoji} **{symbol + ' — ' if symbol else ''}Risk {level}**"
        meta = f"Score: {score:.1f}/10 | Est. max drawdown: {dd:.1f}% | Data quality: {dq:.0f}%"
        reasons = self._get_reasons(result)
        reasons_text = "\n".join(f"  • {r}" for r in reasons) if reasons else "  • Normal market conditions"

        parts = [header, meta, "Reasons:\n" + reasons_text]

        if verbose:
            engine_lines = [f"  {eng}: {float(result.get(eng, 5.0)):.1f}/10" for eng in self._ENGINE_MESSAGES.keys()]
            if engine_lines:
                parts.append("Engine details:\n" + "\n".join(engine_lines))
            bear = result.get("Bear_Factor", 0.5)
            parts.append(f"Bear Factor: {bear:.0%} | Regime: {'Risk-Off' if bear > 0.6 else 'Normal'}")

        return "\n".join(parts)

    def _get_reasons(self, result: Dict[str, Any]) -> List[str]:
        reasons: List[str] = []
        seen: set = set()

        for eng, thr_list in self._ENGINE_MESSAGES.items():
            val = float(result.get(eng, 5.0))
            for thr, msg in thr_list:
                if val > thr and eng not in seen:
                    reasons.append(msg)
                    seen.add(eng)
                    break

        bear_f = float(result.get("Bear_Factor", 0.5))
        if bear_f > 0.70:
            reasons.append(f"Risk-off regime (Fear {bear_f:.0%})")

        dq = float(result.get("DataQuality", 100.0))
        if dq < 40:
            reasons.append(f"Low data quality ({dq:.0f}%) — treat the result with caution")

        return reasons


class RiskComparator:
    def rank(self, results: Union[pd.DataFrame, List[Tuple[str, Dict[str, Any]]]], by: str = "Risk_Score", ascending: bool = True) -> pd.DataFrame:
        if isinstance(results, pd.DataFrame):
            df = results.copy()
        else:
            records = []
            for symbol, r in results:
                record = {"Symbol": symbol}
                record.update(r)
                records.append(record)
            df = pd.DataFrame(records)

        if by not in df.columns:
            by = "Risk_Score"

        return df.sort_values(by, ascending=ascending).reset_index(drop=True)

    def table(self, df: pd.DataFrame, cols: Optional[List[str]] = None) -> str:
        default_cols = ["Symbol", "Risk_Level", "Risk_Score", "Liquidity_Risk", "Volatility_Risk", "Market_Risk", "Expected_Drawdown"]
        show_cols = [c for c in (cols or default_cols) if c in df.columns]

        if not show_cols:
            return "No columns to display"

        header = " | ".join(f"{c:<18}" for c in show_cols)
        sep = "-" * len(header)
        rows = [header, sep]

        for _, row in df.iterrows():
            parts = []
            for c in show_cols:
                val = row.get(c, "")
                if isinstance(val, float):
                    parts.append(f"{val:<18.2f}")
                else:
                    parts.append(f"{str(val):<18}")
            rows.append(" | ".join(parts))

        return "\n".join(rows)

    def find_safest(self, results: Union[pd.DataFrame, List[Tuple[str, Dict[str, Any]]]], max_risk_score: float = 5.0, min_data_quality: float = 50.0) -> pd.DataFrame:
        ranked = self.rank(results)
        mask = (
            (ranked.get("Risk_Score", 10) <= max_risk_score) &
            (ranked.get("DataQuality", 0) >= min_data_quality)
        ) if all(c in ranked.columns for c in ["Risk_Score", "DataQuality"]) else pd.Series([True] * len(ranked))
        return ranked[mask].reset_index(drop=True)


class StressTest:
    def run_scenario(self, row: Union[Dict[str, Any], pd.Series], scenario_name: str) -> Dict[str, Any]:
        if isinstance(row, pd.Series):
            row = row.to_dict()

        scenarios = CONFIG.get("stress_scenarios", {})
        if scenario_name not in scenarios:
            raise ValueError(f"Unknown scenario: {scenario_name}")

        scenario = scenarios[scenario_name]
        stressed_row = copy.deepcopy(row)

        vol_mult = float(scenario.get("volatility_mult", 1.0))
        liq_mult = float(scenario.get("liquidity_mult", 1.0))
        spread_mult = float(scenario.get("spread_mult", 1.0))
        mcap_mult = float(scenario.get("mcap_mult", 1.0))

        for key in ("ATR %", "ATR_pct", "atr_pct", "Historical_Volatility_30d"):
            if is_valid_number(stressed_row.get(key)):
                stressed_row[key] = float(stressed_row[key]) * vol_mult

        for key in ("24h Volume", "Volume", "volume"):
            if is_valid_number(stressed_row.get(key)):
                stressed_row[key] = float(stressed_row[key]) / liq_mult

        for key in ("Spread %", "Spread", "spread_pct"):
            if is_valid_number(stressed_row.get(key)):
                stressed_row[key] = float(stressed_row[key]) * spread_mult

        for key in ("Market Cap", "market_cap", "MarketCap"):
            if is_valid_number(stressed_row.get(key)):
                stressed_row[key] = float(stressed_row[key]) * mcap_mult

        try:
            result = _compute_risk_core(stressed_row)
            return {
                "scenario":        scenario_name,
                "Risk_Score":      result["Risk_Score"],
                "Risk_Level":      result["Risk_Level"],
                "Expected_DD":     result["Expected_Drawdown"],
                "Liquidity_Risk":  result.get("Liquidity_Risk", 5.0),
                "Volatility_Risk": result.get("Volatility_Risk", 5.0),
            }
        except Exception as exc:
            logger.error("Stress test '%s' failed: %s", scenario_name, exc)
            return {"scenario": scenario_name, "error": str(exc)}

    def run_all(self, row: Union[Dict[str, Any], pd.Series]) -> Dict[str, Dict[str, Any]]:
        scenarios = CONFIG.get("stress_scenarios", {})
        return {name: self.run_scenario(row, name) for name in scenarios}

    def worst_case_dd(self, row: Union[Dict[str, Any], pd.Series]) -> float:
        all_results = self.run_all(row)
        dds = [r.get("Expected_DD", 0.0) for r in all_results.values() if "error" not in r]
        return max(dds) if dds else 0.0


class DynamicThresholds:
    def __init__(self, base_thresholds: Optional[Dict[str, float]] = None):
        self._base = base_thresholds or dict(CONFIG["risk_thresholds"])

    def compute(self, bear_factor: float) -> Dict[str, float]:
        bf = _clip(bear_factor, 0.0, 1.0)
        adjustment = 1.0 - 0.15 * (bf - 0.5) * 2
        adjustment = _clip(adjustment, 0.80, 1.10)
        return {k: round(v * adjustment, 2) for k, v in self._base.items()}

    def risk_level_with_regime(self, score: float, bear_factor: float) -> str:
        thresholds = self.compute(bear_factor)
        if score >= thresholds.get("extreme", 8.0): return "Extreme"
        if score >= thresholds.get("high", 6.5): return "High"
        if score >= thresholds.get("medium", 4.5): return "Medium"
        return "Low"


class RiskTimeSeries:
    def compute(self, df: pd.DataFrame, strategy: str = "advanced", window: int = 20) -> pd.DataFrame:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return pd.DataFrame()

        results = []
        records = df.to_dict("records")

        for i, row in enumerate(records):
            try:
                core = _compute_risk_core(row)
                results.append({
                    "Risk_Score":    core["Risk_Score"],
                    "Risk_Level":    core["Risk_Level"],
                    "Volatility_Risk": core.get("Volatility_Risk", 5.0),
                    "Liquidity_Risk":  core.get("Liquidity_Risk", 5.0),
                    "Expected_Drawdown": core.get("Expected_Drawdown", 0.0),
                })
            except Exception as exc:
                logger.warning("RiskTimeSeries row %d failed: %s", i, exc)
                results.append({
                    "Risk_Score": 5.0, "Risk_Level": "Medium",
                    "Volatility_Risk": 5.0, "Liquidity_Risk": 5.0, "Expected_Drawdown": 0.0,
                })

        res_df = pd.DataFrame(results, index=df.index)
        if len(res_df) >= window:
            res_df["Risk_Score_MA"] = res_df["Risk_Score"].rolling(window).mean().round(3)
        else:
            res_df["Risk_Score_MA"] = res_df["Risk_Score"]
        return res_df

    def risk_trend(self, ts: pd.DataFrame) -> str:
        if "Risk_Score_MA" not in ts.columns or len(ts) < 2:
            return "Stable"
        recent = ts["Risk_Score_MA"].dropna()
        if len(recent) < 2:
            return "Stable"
        slope = float(np.polyfit(range(len(recent)), recent.values, 1)[0])
        if slope > 0.05: return "Rising"
        if slope < -0.05: return "Falling"
        return "Stable"


# ════════════════════════════════════════════════════════════
# PUBLIC API
# ════════════════════════════════════════════════════════════

def _as_dict(row: Union[Dict[str, Any], pd.Series]) -> Dict[str, Any]:
    if isinstance(row, pd.Series):
        return row.to_dict()
    return dict(row)


def assess_risk(row: Union[Dict[str, Any], pd.Series], **kwargs) -> str:
    try:
        return _compute_risk_core(_as_dict(row))["Risk_Level"]
    except Exception as e:
        logger.error("assess_risk error: %s", e, exc_info=True)
        return "Unknown"


def compute_risk_score(row: Union[Dict[str, Any], pd.Series], **kwargs) -> float:
    try:
        return _compute_risk_core(_as_dict(row))["Risk_Score"]
    except Exception as e:
        logger.error("compute_risk_score error: %s", e, exc_info=True)
        return 5.0


_BATCH_OUTPUT_COLS: Tuple[str, ...] = (
    "Risk_Score", "Risk_Level", "Risk_Confidence", "Expected_Drawdown",
    "Liquidity_Risk", "Volatility_Risk", "Market_Risk", "Trend_Risk",
    "Technical_Risk", "Volume_Risk", "Derivatives_Risk",
    "Orderbook_Risk", "Onchain_Risk", "DataQuality",
)

_BATCH_DEFAULTS: Dict[str, Any] = {
    "Risk_Score": 5.0, "Risk_Level": "Medium", "Risk_Confidence": 0.0,
    "Expected_Drawdown": 0.0, "Liquidity_Risk": 5.0, "Volatility_Risk": 5.0,
    "Market_Risk": 5.0, "Trend_Risk": 5.0, "Technical_Risk": 5.0,
    "Volume_Risk": 5.0, "Derivatives_Risk": 5.0, "Orderbook_Risk": 5.0,
    "Onchain_Risk": 5.0, "DataQuality": 0.0,
}


def assess_risk_batch(
    df: pd.DataFrame,
    max_workers: Optional[int] = None,
    timeout_sec: Optional[int] = None,
    use_threads: Optional[bool] = None,
    use_cache: bool = False,
    **kwargs,
) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()

    records = df.to_dict("records")
    threads = CONFIG["batch_use_threads"] if use_threads is None else bool(use_threads)
    cache = _global_cache if use_cache else None

    def _process(rec: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if cache is not None:
                return cache.get_or_compute(rec, _compute_risk_core)
            return _compute_risk_core(rec)
        except Exception as exc:
            logger.warning("risk row failed: %s", exc)
            return dict(_BATCH_DEFAULTS)

    if threads:
        from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
        workers = int(max_workers or CONFIG["batch_max_workers"])
        t_out = float(timeout_sec or CONFIG["batch_timeout_sec"])

        results: List[Dict[str, Any]] = [dict(_BATCH_DEFAULTS)] * len(records)

        try:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(_process, rec): i for i, rec in enumerate(records)}

                try:
                    for future in as_completed(futures, timeout=t_out):
                        idx = futures[future]
                        try:
                            results[idx] = future.result()
                        except Exception as exc:
                            logger.warning("Thread row %d failed: %s", idx, exc)
                except FuturesTimeoutError:
                    logger.error("assess_risk_batch timed out after %s seconds. Canceling pending tasks.", t_out)
                    for future in futures:
                        if not future.done():
                            future.cancel()

        except Exception as e:
            logger.error("Thread execution failed: %s", e)
    else:
        results = [_process(rec) for rec in records]

    out = df.copy()
    result_df = pd.DataFrame(results, index=df.index)

    for col in _BATCH_OUTPUT_COLS:
        default = _BATCH_DEFAULTS.get(col, 5.0)
        if col in result_df.columns:
            out[col] = result_df[col].fillna(default)
        else:
            out[col] = default

    levels = out["Risk_Level"].value_counts().to_dict()
    logger.info("assess_risk_batch: %d rows | %s", len(records), " | ".join(f"{k}={v}" for k, v in levels.items()))
    return out


_DETAIL_THRESHOLDS: Tuple[Tuple[str, float, str], ...] = (
    ("Volatility_Risk",  8.5, "Extreme volatility"),
    ("Volatility_Risk",  7.0, "High volatility — size management required"),
    ("Liquidity_Risk",   7.5, "Poor liquidity — exit slippage risk"),
    ("Liquidity_Risk",   6.5, "Below-average liquidity"),
    ("Market_Risk",      7.5, "Small market cap — whale manipulation risk"),
    ("Market_Risk",      6.0, "Limited market cap"),
    ("Trend_Risk",       7.0, "Unstable / overextended trend"),
    ("Technical_Risk",   7.0, "Technical indicators at extreme"),
    ("Volume_Risk",      7.0, "Abnormal volume — possible manipulation"),
    ("Derivatives_Risk", 7.5, "Crowded funding / long-short imbalance"),
    ("Orderbook_Risk",   7.0, "Order book imbalance detected"),
    ("Onchain_Risk",     7.0, "On-chain signals warning"),
)


def assess_risk_details(
    row: Union[Dict[str, Any], pd.Series],
    include_stress: bool = False,
    **kwargs,
) -> Dict[str, Any]:
    explainer = RiskExplainer()

    try:
        core = _compute_risk_core(_as_dict(row))
    except Exception as e:
        logger.error("assess_risk_details error: %s", e, exc_info=True)
        return {
            "level":      "Unknown",
            "score":      50.0,
            "reasons":    [f"Calculation error: {e}"],
            "Risk_Score": 5.0,
            "Risk_Level": "Unknown",
            "DataQuality": 0.0,
        }

    reasons = explainer._get_reasons(core)

    stress_results: Dict[str, Any] = {}
    if include_stress:
        try:
            stress = StressTest()
            stress_results = stress.run_all(_as_dict(row))
        except Exception as exc:
            logger.warning("Stress test failed: %s", exc)

    result = {
        "level":  core["Risk_Level"],
        "score":  round(core["Risk_Score"] * 10.0, 2),
        "reasons": reasons,
        **core,
    }

    if stress_results:
        result["stress_scenarios"] = stress_results

    result.pop("_engines", None)

    return result


__all__ = [
    "assess_risk",
    "compute_risk_score",
    "assess_risk_batch",
    "assess_risk_details",
    "is_valid_number",
    "CONFIG",
    "FEATURE_META_DIRECTION",
    "FEATURE_NORM_RANGES",
    "FEATURE_LOG_SCALE",
    "EngineResult",
    "FactorSpec",
    "RiskSummary",
    "RiskCache",
    "RiskExplainer",
    "RiskComparator",
    "StressTest",
    "DynamicThresholds",
    "RiskTimeSeries",
]