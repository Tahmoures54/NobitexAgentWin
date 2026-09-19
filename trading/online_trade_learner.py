"""Lightweight online learner for pump/trend trade-quality estimation.

The learner is deliberately conservative: it estimates the probability that a
candidate will reach a positive net outcome from scanner/order-flow features.
It does not change position sizing, stops, or live execution by itself.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Mapping, Sequence


class OnlineTradeLearner:
    """Online logistic regression with bounded weights and JSON persistence."""

    VERSION = 1

    def __init__(
        self,
        path: str = "data/online_trade_model.json",
        learning_rate: float = 0.05,
        l2: float = 0.0005,
        min_samples: int = 30,
        default_probability: float = 0.5,
    ) -> None:
        self.path = Path(path)
        self.learning_rate = float(learning_rate)
        self.l2 = float(l2)
        self.min_samples = int(min_samples)
        self.default_probability = float(default_probability)
        self.samples = 0
        self.weights: dict[str, float] = {}
        self.bias = 0.0
        self.feature_means: dict[str, float] = {}
        self.feature_scales: dict[str, float] = {}
        self._load()

    @staticmethod
    def _sigmoid(value: float) -> float:
        value = max(-35.0, min(35.0, value))
        return 1.0 / (1.0 + math.exp(-value))

    @staticmethod
    def _clean_features(features: Mapping[str, object]) -> dict[str, float]:
        cleaned: dict[str, float] = {}
        for key, value in features.items():
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                cleaned[str(key)] = number
        return cleaned

    def _normalise(self, features: Mapping[str, object]) -> dict[str, float]:
        cleaned = self._clean_features(features)
        result: dict[str, float] = {}
        for key, value in cleaned.items():
            mean = self.feature_means.get(key, value)
            scale = self.feature_scales.get(key, 1.0)
            if scale <= 1e-9:
                scale = 1.0
            result[key] = max(-6.0, min(6.0, (value - mean) / scale))
        return result

    def _update_stats(self, features: Mapping[str, float]) -> None:
        # Running mean and variance are intentionally simple and robust for a
        # small local trading dataset.
        for key, value in features.items():
            old_mean = self.feature_means.get(key, value)
            old_scale = self.feature_scales.get(key, 1.0)
            if self.samples <= 1:
                self.feature_means[key] = value
                self.feature_scales[key] = max(old_scale, 1.0)
                continue
            delta = value - old_mean
            new_mean = old_mean + delta / self.samples
            variance_proxy = max(old_scale * old_scale, 1e-6)
            variance_proxy = ((self.samples - 2) * variance_proxy + delta * (value - new_mean)) / max(self.samples - 1, 1)
            self.feature_means[key] = new_mean
            self.feature_scales[key] = max(math.sqrt(abs(variance_proxy)), 1.0)

    def predict_probability(self, features: Mapping[str, object]) -> float:
        if self.samples < self.min_samples:
            return self.default_probability
        x = self._normalise(features)
        score = self.bias
        for key, value in x.items():
            score += self.weights.get(key, 0.0) * value
        return self._sigmoid(score)

    def accept(self, features: Mapping[str, object], profitable: bool) -> float:
        """Train on one closed trade and return the post-update probability."""
        raw = self._clean_features(features)
        self.samples += 1
        self._update_stats(raw)
        x = self._normalise(raw)
        target = 1.0 if profitable else 0.0
        probability = self._sigmoid(
            self.bias + sum(self.weights.get(k, 0.0) * v for k, v in x.items())
        )
        error = target - probability

        self.bias += self.learning_rate * error
        for key, value in x.items():
            weight = self.weights.get(key, 0.0)
            weight += self.learning_rate * (error * value - self.l2 * weight)
            self.weights[key] = max(-4.0, min(4.0, weight))
        self.save()
        return self.predict_probability(raw)

    def state(self) -> dict:
        return {
            "version": self.VERSION,
            "samples": self.samples,
            "bias": self.bias,
            "weights": self.weights,
            "feature_means": self.feature_means,
            "feature_scales": self.feature_scales,
            "learning_rate": self.learning_rate,
            "l2": self.l2,
            "min_samples": self.min_samples,
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.state(), indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if payload.get("version") != self.VERSION:
            return
        self.samples = int(payload.get("samples", 0))
        self.bias = float(payload.get("bias", 0.0))
        self.weights = {str(k): float(v) for k, v in payload.get("weights", {}).items()}
        self.feature_means = {str(k): float(v) for k, v in payload.get("feature_means", {}).items()}
        self.feature_scales = {str(k): max(float(v), 1.0) for k, v in payload.get("feature_scales", {}).items()}


def trade_features_from_signal(signal: Mapping[str, object]) -> dict[str, float]:
    """Extract a stable feature vector from scanner/order-flow output."""
    aliases = {
        "pump_pct": ("PumpPct", "pump_pct", "MovementPct", "movement_pct", "ObservedMovePct"),
        "momentum": ("Momentum", "momentum", "MomentumScore"),
        "volume": ("Volume24h", "volume_24h", "Volume", "volume"),
        "spread_pct": ("SpreadPct", "spread_pct", "OrderFlowSpreadPct"),
        "order_flow_score": ("OrderFlowScore", "order_flow_score"),
        "order_flow_imbalance": ("OrderFlowImbalance", "order_flow_imbalance"),
        "confidence": ("Confidence", "confidence", "Quality"),
        "trend_score": ("TrendScore", "trend_score"),
        "chase_pct": ("ChasePct", "chase_pct"),
        "btc_change_pct": ("BTCChangePct", "btc_change_pct"),
    }
    result: dict[str, float] = {}
    for target, keys in aliases.items():
        for key in keys:
            if key in signal:
                try:
                    result[target] = float(signal[key])
                    break
                except (TypeError, ValueError):
                    pass
    return result
