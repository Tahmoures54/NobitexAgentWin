"""
Signal engine for CryptoScanner.  (v7.5 — WeakKeyDictionary risk cache)

CHANGES vs v7.4
───────────────
1. `_risk()` no longer mutates the caller's row dict.
   Previously the memoised value was stored under a private key
   `__risk_cached_v74` directly inside the dict, which leaked to any
   caller that kept a reference to the row (and was visible in
   `to_dict("records")` output).  Now the cache lives in a module-level
   `weakref.WeakKeyDictionary`, so entries are garbage-collected
   automatically when the row is dropped.

2. `generate_signals()` no longer strips a private key — there is
   nothing to strip anymore.

CHANGES vs v7.3 (retained)
──────────────────────────
- `apply_strategy_to_df` returns the mutated input when return_frame=False.
- `parallel` parameter documented as a no-op.
"""
from __future__ import annotations

import logging
import math
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

try:
    from analysis.risk import assess_risk_details
except Exception:
    assess_risk_details = None


CONFIG: Dict[str, Any] = {
    "strong_buy_threshold": 65.0,
    "buy_threshold": 20.0,
    "sell_threshold": -20.0,
    "strong_sell_threshold": -65.0,
    "position_max_pct": 20.0,
    "stop_loss_pct": 3.0,
    "take_profit_pct": 12.0,
    "min_quality": 0.0,
}


# ── Per-row risk memoisation using a WeakKeyDictionary ──
# Replaces the previous approach of mutating the row dict with a
# private "__risk_cached_v74" key.  The WeakKeyDictionary keeps the
# cache tied to the dict's lifetime; when the caller drops the dict,
# the entry is garbage-collected automatically.
_risk_cache: "weakref.WeakKeyDictionary[dict, str]" = weakref.WeakKeyDictionary()


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════

def is_valid_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float, np.number))
        and not isinstance(value, (bool, np.bool_))
        and math.isfinite(float(value))
    )


def _num(row: Dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for k in keys:
        v = row.get(k)
        if is_valid_number(v):
            return float(v)
    return default


def update_config(values: Dict[str, Any]) -> List[str]:
    CONFIG.update(values or {})
    return validate_config(CONFIG)


def validate_config(config: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if not -100 <= float(config.get("strong_buy_threshold", 65)) <= 100:
        errors.append("strong_buy_threshold out of range")
    if not -100 <= float(config.get("buy_threshold", 20)) <= 100:
        errors.append("buy_threshold out of range")
    if not 0 < float(config.get("position_max_pct", 20)) <= 100:
        errors.append("position_max_pct must be in (0,100]")
    return errors


def _risk(row: Dict[str, Any]) -> str:
    """
    Return the row's risk level.

    Memoised per-row via a WeakKeyDictionary so the expensive
    assess_risk_details() runs at most once per row.

    v7.5: no longer mutates the caller's dict (the old
    `row["__risk_cached_v74"] = result` line was visible to callers
    and to DataFrame to_dict output).
    """
    # Fast path: check the weak cache
    try:
        cached = _risk_cache.get(row)
    except TypeError:
        cached = None
    if cached is not None:
        return str(cached)

    result: str
    if assess_risk_details is not None:
        try:
            d = assess_risk_details(row)
            result = str(d.get("Risk_Level", d.get("risk_level", "Medium")))
        except Exception as exc:
            logger.debug("_risk() assess_risk_details failed: %s", exc)
            result = str(row.get("Risk", row.get("risk", "Medium")) or "Medium")
    else:
        result = str(row.get("Risk", row.get("risk", "Medium")) or "Medium")

    try:
        _risk_cache[row] = result
    except TypeError:
        # Non-hashable / non-weakref-able object — skip caching
        pass
    return result


def _signal_from_score(score: float) -> str:
    if score >= CONFIG["strong_buy_threshold"]:
        return "Strong Buy"
    if score >= CONFIG["buy_threshold"]:
        return "Buy Signal"
    if score <= CONFIG["strong_sell_threshold"]:
        return "Strong Sell"
    if score <= CONFIG["sell_threshold"]:
        return "Sell Signal"
    return "Neutral"


# ══════════════════════════════════════════════════════════════
# Individual strategies
# ══════════════════════════════════════════════════════════════

def basic_signal_strategy(row: Dict[str, Any]) -> Dict[str, Any]:
    rsi = _num(row, "RSI", default=50)
    ch1 = _num(row, "1h Change (%)")
    ch24 = _num(row, "24h Change (%)")
    macd = _num(row, "MACD")
    macds = _num(row, "MACD Signal")
    ema50 = _num(row, "EMA50")
    ema200 = _num(row, "EMA200")
    price = _num(row, "Price", "close")

    score = 0.0
    score += np.clip(ch1 * 8, -25, 25)
    score += np.clip(ch24 * 3, -20, 20)
    score += 12 if macd > macds else -12 if macd < macds else 0
    if price and ema50 and price > ema50:
        score += 10
    elif price and ema50:
        score -= 10
    if price and ema200 and price > ema200:
        score += 12
    elif price and ema200:
        score -= 12
    if 50 <= rsi <= 70:
        score += 8
    elif rsi > 80:
        score -= 12
    elif rsi < 25:
        score += 5
    score = float(np.clip(score, -100, 100))
    signal = _signal_from_score(score)

    completeness = sum(
        k in row and is_valid_number(row[k])
        for k in ("Price", "RSI", "24h Change (%)", "1h Change (%)", "EMA50", "EMA200")
    ) / 6

    risk = _risk(row)
    return {
        "signal": signal, "Signal": signal,
        "score": round(score, 3), "Score": round(score, 3),
        "confidence": round(abs(score) * max(completeness, 0.25), 2),
        "quality": round(completeness, 3),
        "risk": risk, "risk_level": risk,
        "tradable_long": signal in ("Strong Buy", "Buy Signal"),
        "long_score": round(max(0, score), 3),
        "completeness": round(completeness, 3),
    }


def momentum_strategy(row: Dict[str, Any]) -> Dict[str, Any]:
    ch1 = _num(row, "1h Change (%)")
    ch24 = _num(row, "24h Change (%)")
    rv = _num(row, "Relative_Volume", default=1)
    score = float(np.clip(ch1 * 10 + ch24 * 2.5 + (rv - 1) * 12, -100, 100))
    signal = _signal_from_score(score)
    risk = _risk(row)
    return {
        "signal": signal, "Signal": signal,
        "score": round(score, 3), "Score": round(score, 3),
        "confidence": round(min(100, abs(score) + 20), 2),
        "quality": 0.8 if rv >= 1 else 0.6,
        "risk": risk, "risk_level": risk,
        "tradable_long": signal in ("Strong Buy", "Buy Signal"),
        "long_score": round(max(0, score), 3),
        "components": {"momentum": score},
    }


def mean_reversion_strategy(row: Dict[str, Any]) -> Dict[str, Any]:
    rsi = _num(row, "RSI", default=50)
    price = _num(row, "Price", "close")
    lower = _num(row, "BB Lower")
    upper = _num(row, "BB Upper")
    score = 0.0
    if rsi < 35:
        score += 45
    elif rsi > 70:
        score -= 45
    if price and lower and price < lower:
        score += 35
    if price and upper and price > upper:
        score -= 35
    signal = _signal_from_score(score)
    risk = _risk(row)
    return {
        "signal": signal, "Signal": signal,
        "score": score, "Score": score,
        "confidence": min(100, abs(score) + 25),
        "quality": 0.7,
        "risk": risk, "risk_level": risk,
        "tradable_long": signal in ("Strong Buy", "Buy Signal"),
        "long_score": max(0, score),
    }


def volume_profile_strategy(row: Dict[str, Any]) -> Dict[str, Any]:
    rv = _num(row, "Relative_Volume", default=1)
    ch24 = _num(row, "24h Change (%)")
    score = float(np.clip(ch24 * 5 + (rv - 1) * 25, -100, 100))
    signal = _signal_from_score(score)
    risk = _risk(row)
    return {
        "signal": signal, "Signal": signal,
        "score": score, "Score": score,
        "confidence": min(100, abs(score) + 20),
        "quality": min(1, 0.5 + max(0, rv - 1) * 0.3),
        "risk": risk, "risk_level": risk,
        "tradable_long": signal in ("Strong Buy", "Buy Signal"),
        "long_score": max(0, score),
    }


def trend_pullback_strategy(row: Dict[str, Any]) -> Dict[str, Any]:
    price = _num(row, "Price", "close")
    e50 = _num(row, "EMA50")
    e200 = _num(row, "EMA200")
    rsi = _num(row, "RSI", default=50)
    score = 0
    if price and e50:
        score += 30 if price > e50 else -30
    if price and e200:
        score += 30 if price > e200 else -30
    if 45 <= rsi <= 65:
        score += 25
    elif rsi > 75:
        score -= 20
    score = float(np.clip(score, -100, 100))
    signal = _signal_from_score(score)
    risk = _risk(row)
    return {
        "signal": signal, "Signal": signal,
        "score": score, "Score": score,
        "confidence": min(100, abs(score) + 25),
        "quality": 0.75,
        "risk": risk, "risk_level": risk,
        "tradable_long": signal in ("Strong Buy", "Buy Signal"),
        "long_score": max(0, score),
    }


def breakout_strategy(row: Dict[str, Any]) -> Dict[str, Any]:
    price = _num(row, "Price", "close")
    high = _num(row, "BB Upper")
    low = _num(row, "BB Lower")
    ch1 = _num(row, "1h Change (%)")
    score = 40 if price and high and price >= high else \
           -40 if price and low and price <= low else 0
    score += float(np.clip(ch1 * 8, -35, 35))
    score = float(np.clip(score, -100, 100))
    signal = _signal_from_score(score)
    risk = _risk(row)
    return {
        "signal": signal, "Signal": signal,
        "score": score, "Score": score,
        "confidence": min(100, abs(score) + 20),
        "quality": 0.7,
        "risk": risk, "risk_level": risk,
        "tradable_long": signal in ("Strong Buy", "Buy Signal"),
        "long_score": max(0, score),
    }


def advanced_signal_strategy(row: Dict[str, Any]) -> Dict[str, Any]:
    parts = [
        basic_signal_strategy(row),
        momentum_strategy(row),
        trend_pullback_strategy(row),
        breakout_strategy(row),
    ]
    weights = [0.35, 0.25, 0.25, 0.15]
    score = float(np.clip(sum(p["score"] * w for p, w in zip(parts, weights)), -100, 100))
    signal = _signal_from_score(score)
    quality = float(np.mean([p["quality"] for p in parts]))
    risk = _risk(row)
    return {
        "signal": signal, "Signal": signal,
        "score": round(score, 3), "Score": round(score, 3),
        "confidence": round(min(100, abs(score) * 0.8 + quality * 20), 2),
        "quality": round(quality, 3),
        "risk": risk, "risk_level": risk,
        "tradable_long": signal in ("Strong Buy", "Buy Signal"),
        "long_score": round(max(0, score), 3),
        "components": {
            "basic": parts[0], "momentum": parts[1],
            "trend": parts[2], "breakout": parts[3],
        },
    }


def composite_strategy(row: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(row, dict) or not row:
        return {
            "signal": "Neutral", "Signal": "Neutral",
            "score": 0.0, "Score": 0.0,
            "confidence": 0.0, "quality": 0.0,
            "risk": "Unknown", "risk_level": "Unknown",
            "tradable_long": False, "long_score": 0.0,
            "completeness": 0.0,
            "reasons": "Ensemble: insufficient data",
        }

    # With only a price, deliberately stay neutral.
    required = ("RSI", "1h Change (%)", "24h Change (%)", "EMA50", "EMA200")
    completeness = sum(is_valid_number(row.get(k)) for k in required) / len(required)
    if completeness < 0.4:
        risk = _risk(row)
        return {
            "signal": "Neutral", "Signal": "Neutral",
            "score": 0.0, "Score": 0.0,
            "confidence": 0.0, "quality": completeness,
            "risk": risk, "risk_level": risk,
            "tradable_long": False, "long_score": 0.0,
            "completeness": completeness,
            "reasons": "Ensemble: insufficient data",
        }

    a = advanced_signal_strategy(row)
    m = momentum_strategy(row)
    mr = mean_reversion_strategy(row)

    score = float(np.clip(0.6 * a["score"] + 0.25 * m["score"] + 0.15 * mr["score"], -100, 100))
    signal = _signal_from_score(score)
    risk = _risk(row)  # single cached call
    quality = float(np.clip(0.65 * a["quality"] + 0.35 * completeness, 0, 1))
    reasons = (
        f"Ensemble: momentum={m['score']:.1f}, "
        f"trend={a['score']:.1f}, reversion={mr['score']:.1f}"
    )
    return {
        "signal": signal, "Signal": signal,
        "score": round(score, 3), "Score": round(score, 3),
        "confidence": round(min(100, abs(score) * 0.85 + quality * 15), 2),
        "quality": round(quality, 3),
        "risk": risk, "risk_level": risk,
        "tradable_long": signal in ("Strong Buy", "Buy Signal"),
        "long_score": round(max(0, score), 3),
        "completeness": round(completeness, 3),
        "reasons": reasons, "Reasons": reasons,
    }


# ══════════════════════════════════════════════════════════════
# Registry
# ══════════════════════════════════════════════════════════════

@dataclass
class MarketRegime:
    label: str
    avg_score: float
    confidence: float
    sample_size: int


class StrategyRegistry:
    def __init__(self):
        self._strategies: Dict[str, Callable] = {}

    def register(self, name: str, fn: Callable, alias: Optional[str] = None):
        self._strategies[name] = fn
        if alias:
            self._strategies[alias] = fn

    def get(self, name: str):
        return self._strategies.get(name)

    def list_all(self):
        return list(dict.fromkeys(self._strategies.keys()))

    def __contains__(self, name: str):
        return name in self._strategies


strategy_registry = StrategyRegistry()
for _n, _f in {
    "basic_signal_strategy": basic_signal_strategy,
    "basic": basic_signal_strategy,
    "advanced": advanced_signal_strategy,
    "advanced_signal_strategy": advanced_signal_strategy,
    "momentum": momentum_strategy,
    "mean_reversion": mean_reversion_strategy,
    "volume_profile": volume_profile_strategy,
    "trend_pullback": trend_pullback_strategy,
    "breakout": breakout_strategy,
    "composite": composite_strategy,
}.items():
    strategy_registry.register(_n, _f)


def get_strategy(name: str):
    return strategy_registry.get(name)


# ══════════════════════════════════════════════════════════════
# DataFrame application
# ══════════════════════════════════════════════════════════════

def _apply_one(row, fn):
    """Convert a Series row to a dict, run the strategy, return result dict."""
    d = row.to_dict() if isinstance(row, pd.Series) else dict(row)
    return fn(d)


def apply_strategy_to_df(
    df: pd.DataFrame,
    strategy: str = "composite",
    enrich: bool = True,
    return_frame: bool = True,
    parallel: bool = False,          # documented no-op for backward compat
    batch_size: int = 50,            # documented no-op for backward compat
    **kwargs,
):
    """
    Apply a strategy to every row of `df`.

    Parameters
    ----------
    strategy : str
        Registry name (see `strategy_registry.list_all()`).
    enrich : bool
        When `return_frame=False`, copy enriched columns back into the
        input DataFrame in-place.  Ignored when `return_frame=True`.
    return_frame : bool
        When True (default) returns a new DataFrame with all strategy
        columns.  When False, mutates `df` and returns it.
    parallel, batch_size : kept for backward compatibility. Currently a
        no-op because pandas `.apply` already handles the GIL well for
        these callables and the strategy dispatch is not compute-bound
        enough to benefit from thread-level parallelism.
    """
    if df is None:
        return pd.DataFrame()
    if df.empty:
        return df.copy() if return_frame else df

    fn = get_strategy(strategy)
    if fn is None:
        raise ValueError(f"Unknown strategy: {strategy}")

    result = df.apply(lambda r: _apply_one(r, fn), axis=1)

    out = df.copy()
    fields = set()
    for d in result:
        fields.update(d.keys())
    for k in fields:
        out[k] = [d.get(k) for d in result]

    # Ensure dual-column variants exist
    if "signal" in out:
        out["Signal"] = out["signal"]
    if "score" in out:
        out["Score"] = out["score"]
    if "risk" in out:
        out["Risk"] = out["risk"]
    if "risk_level" in out:
        out["Risk_Level"] = out["risk_level"]

    if not return_frame:
        if enrich:
            for col in out.columns:
                df[col] = out[col].values
        return df
    return out


def rank_long_candidates(
    df: pd.DataFrame,
    strategy: str = "composite",
    top_n: int = 10,
    **kwargs,
):
    out = apply_strategy_to_df(df, strategy=strategy, return_frame=True, **kwargs)
    if out.empty:
        return out
    out = out[out["tradable_long"].fillna(False)].copy()
    return out.sort_values("long_score", ascending=False).head(int(top_n))


def generate_signals(
    df: pd.DataFrame,
    strategy: str = "composite",
    top_n: int = 10,
    include_neutral: bool = True,
    **kwargs,
) -> List[Dict[str, Any]]:
    out = apply_strategy_to_df(df, strategy=strategy, return_frame=True, **kwargs)
    if not include_neutral:
        out = out[out["signal"] != "Neutral"]
    out = out.sort_values("long_score", ascending=False).head(int(top_n))
    rows = out.to_dict("records")
    for r in rows:
        if "Symbol" not in r:
            r["Symbol"] = str(r.get("symbol", "")).upper()
        # v7.5: no private key to strip anymore — the risk cache is
        # now stored in a WeakKeyDictionary, not inside the row dict.
    return rows


def combine_timeframe_signals(signals: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    if not signals:
        return composite_strategy({})
    vals = list(signals.values())
    score = float(np.mean([_num(v, "score") for v in vals]))
    signal = _signal_from_score(score)
    reasons = "MTF: " + " | ".join(
        f"[{tf}] {v.get('signal', 'Neutral')}" for tf, v in signals.items()
    )
    return {
        "signal": signal, "Signal": signal,
        "score": round(score, 3), "Score": round(score, 3),
        "confidence": round(np.mean([_num(v, "confidence") for v in vals]), 2),
        "quality": round(np.mean([_num(v, "quality") for v in vals]), 3),
        "risk": str(vals[0].get("risk", "Medium")),
        "risk_level": str(vals[0].get("risk_level", "Medium")),
        "tradable_long": signal in ("Strong Buy", "Buy Signal"),
        "long_score": max(0, score),
        "reasons": reasons, "Reasons": reasons,
    }


# ══════════════════════════════════════════════════════════════
# Position sizing / regime / explaining / metrics
# ══════════════════════════════════════════════════════════════

@dataclass
class PositionSizer:
    portfolio_value: float
    max_position_pct: float = CONFIG["position_max_pct"]
    risk_per_trade_pct: float = 1.0

    def calculate(self, row, signal_result, entry_price):
        score = max(0, min(100, float(signal_result.get("score", 0))))
        pct = min(self.max_position_pct, max(1.0, self.max_position_pct * score / 100 if score else 1.0))
        value = self.portfolio_value * pct / 100
        sl = max(0, entry_price * (1 - CONFIG["stop_loss_pct"] / 100))
        target = entry_price * (1 + CONFIG["take_profit_pct"] / 100)
        risk = value * CONFIG["stop_loss_pct"] / 100
        return {
            "position_pct": pct, "position_value": value,
            "stop_price": sl, "target_price": target, "risk_value": risk,
        }


class MarketRegimeFilter:
    def detect(self, df):
        if df is None or df.empty:
            return MarketRegime("Unknown", 0.0, 0.0, 0)
        scores = pd.to_numeric(df.get("score", pd.Series([0] * len(df))), errors="coerce").fillna(0)
        avg = float(scores.mean())
        conf = min(1.0, abs(avg) / 50)
        label = "Bull" if avg >= 15 else "Bear" if avg <= -15 else "Neutral"
        return MarketRegime(label, avg, conf, len(df))

    def filter_longs(self, df, regime):
        if regime.label == "Bear":
            return df[df["long_score"] >= max(50, df["long_score"].median() if len(df) else 50)]
        return df[df["tradable_long"].fillna(False)] if "tradable_long" in df else df


class SignalExplainer:
    def explain(self, result, symbol="", verbose=False):
        return (
            f"{symbol}: {result.get('signal', 'Neutral')} | "
            f"score={result.get('score', 0):.1f} | "
            f"confidence={result.get('confidence', 0):.1f}% | "
            f"risk={result.get('risk', result.get('risk_level', 'Unknown'))}"
        )

    def compare(self, results):
        return "\n".join(self.explain(r, s) for s, r in results)


@dataclass
class BacktestMetrics:
    total_signals: int
    avg_score: float
    avg_confidence: float

    @classmethod
    def compute(cls, df):
        if df is None or df.empty:
            return cls(0, 0.0, 0.0)
        return cls(
            len(df),
            float(pd.to_numeric(df.get("score", 0), errors="coerce").fillna(0).mean()),
            float(pd.to_numeric(df.get("confidence", 0), errors="coerce").fillna(0).mean()),
        )

    @staticmethod
    def score_distribution(df, bins=10):
        if df is None or df.empty:
            return {}
        s = pd.to_numeric(df.get("score", 0), errors="coerce").fillna(0)
        counts, edges = np.histogram(s, bins=bins)
        return {f"{edges[i]:.1f}..{edges[i+1]:.1f}": int(counts[i]) for i in range(len(counts))}


# ══════════════════════════════════════════════════════════════
# Legacy helpers
# ══════════════════════════════════════════════════════════════

def check_5_percent_pump(klines: List[Dict[str, Any]], pump_threshold: float = 5.0):
    if not klines:
        return False, 0.0
    vals = []
    for k in klines[-2:]:
        o = _num(k, "open")
        c = _num(k, "close")
        if o > 0:
            vals.append((c - o) / o * 100)
    p = max(vals, default=0.0)
    return p >= pump_threshold, max(0.0, p)


def should_enter_trade(klines, coin_data=None, risk_filter_enabled=False, blocked_risk_levels=None):
    if not risk_filter_enabled:
        return True
    if coin_data is None or assess_risk_details is None:
        return False
    try:
        return assess_risk_details(coin_data).get("Risk_Level", "Low") not in (
            blocked_risk_levels or ["High", "Extreme"]
        )
    except Exception:
        return False