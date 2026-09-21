"""Nobitex IRT raw-scan trend engine.

The engine is an entry filter, not a predictor.  In strict/raw mode every BUY
candidate is backed by the same scanner-owned price history used by
``analysis.raw_trend``: a move strictly above the user threshold is the
entry gate.  Streak, higher highs/lows and previous-mean flags still
participate as a small ranking bonus.  Protective stop-loss and trailing
stop percentages are attached to every candidate.  No technical indicator,
forecast or model is consulted.

The small legacy path is retained for old callers that instantiate the engine
with only the v6 ``min_observed_move_pct`` arguments.  The shipped BotConfig
and GUI explicitly enable ``raw_scan_trend_enabled``.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

from analysis.raw_trend import TrendAssessment, assess_trend
from core.utils import safe_float
from trading.scan_history import ScanHistoryStore

STABLES = {"USDT", "USDC", "USD", "DAI", "TUSD", "FDUSD", "USDE", "PYUSD", "IRT", "RLS", "IRR"}
_BTC_PROXIES = {"BTC", "WBTC", "TBTC"}


def _pct_change(new: float, old: float) -> Optional[float]:
    if old <= 0 or new <= 0:
        return None
    return (float(new) - float(old)) / float(old) * 100.0


class NobitexMomentumEngine:
    """Stateful raw price-action filter for the Nobitex IRT spot book."""

    def __init__(
        self,
        *,
        pump_threshold_pct: float = 1.5,
        max_spread_pct: float = 0.9,
        min_volume_irt: float = 300_000_000.0,
        max_local_24h_pct: float = 15.0,
        max_local_fall_pct: float = 0.8,
        max_chase_pct: float = 0.8,
        movement_lookback_scans: int = 4,
        min_confirm_scans: int = 1,
        min_observed_move_pct: float = 0.8,
        min_ask_depth_quote: float = 300_000.0,
        history_len: int = 60,
        btc_dump_exception_enabled: bool = True,
        btc_max_dump_pct: float = 2.5,
        eagle_min_observed_move_pct: float = 2.5,
        eagle_min_1h_pct: float = 2.0,
        eagle_min_volume_irt: float = 300_000_000.0,
        eagle_max_spread_pct: float = 0.9,
        # 6.1 canonical names
        threshold_percent: Optional[float] = None,
        min_consecutive_positive_scans: Optional[int] = None,
        trend_lookback_scans: Optional[int] = None,
        stop_loss_percent: float = 3.0,
        trailing_stop_percent: float = 0.0,
        raw_scan_trend_enabled: bool = False,
        require_trend_structure: Optional[bool] = None,
        symbol_whitelist: Optional[Iterable[str]] = None,
        symbol_blacklist: Optional[Iterable[str]] = None,
        scan_interval_seconds: int = 10,
        cooldown_minutes: int = 30,
        history_file: Optional[str] = None,
        **_,
    ):
        # Presence of a canonical argument is an explicit request for the
        # strict strategy.  This preserves the old direct-engine API while
        # making the new settings impossible to accidentally ignore.
        canonical_requested = any(
            value is not None
            for value in (threshold_percent, min_consecutive_positive_scans, trend_lookback_scans)
        ) or bool(raw_scan_trend_enabled)
        self.raw_scan_trend_enabled = bool(raw_scan_trend_enabled or canonical_requested)
        self.require_trend_structure = (
            self.raw_scan_trend_enabled if require_trend_structure is None else bool(require_trend_structure)
        )

        self.threshold_percent = max(
            0.0,
            float(threshold_percent if threshold_percent is not None else min_observed_move_pct),
        )
        self.min_consecutive_positive_scans = max(
            1,
            int(
                min_consecutive_positive_scans
                if min_consecutive_positive_scans is not None
                else (min_confirm_scans if canonical_requested else 1)
            ),
        )
        self.trend_lookback_scans = max(
            4,
            int(trend_lookback_scans if trend_lookback_scans is not None else movement_lookback_scans),
        )
        self.stop_loss_percent = max(0.0, float(stop_loss_percent))
        self.trailing_stop_percent = max(0.0, float(trailing_stop_percent))
        self.scan_interval_seconds = max(1, int(scan_interval_seconds))
        self.cooldown_minutes = max(0, int(cooldown_minutes))
        self.symbol_whitelist = self._normalise_symbols(symbol_whitelist)
        self.symbol_blacklist = self._normalise_symbols(symbol_blacklist)

        # Legacy aliases are kept visible in diagnostics and old integrations.
        self.pump_threshold_pct = max(0.1, float(pump_threshold_pct))
        self.min_observed_move_pct = self.threshold_percent
        self.max_spread_pct = max(0.05, float(max_spread_pct))
        self.min_volume_irt = max(0.0, float(min_volume_irt))
        self.max_local_24h_pct = float(max_local_24h_pct)
        self.max_local_fall_pct = max(0.0, float(max_local_fall_pct))
        self.max_chase_pct = max(0.0, float(max_chase_pct))
        self.movement_lookback_scans = self.trend_lookback_scans
        self.min_confirm_scans = max(1, int(min_confirm_scans))
        self.min_ask_depth_quote = max(0.0, float(min_ask_depth_quote))
        self.history_len = max(8, int(history_len))
        self.btc_dump_exception_enabled = bool(btc_dump_exception_enabled)
        self.btc_max_dump_pct = max(0.0, float(btc_max_dump_pct))
        self.eagle_min_observed_move_pct = max(0.0, float(eagle_min_observed_move_pct))
        self.eagle_min_1h_pct = max(0.0, float(eagle_min_1h_pct))
        self.eagle_min_volume_irt = max(0.0, float(eagle_min_volume_irt))
        self.eagle_max_spread_pct = max(0.05, float(eagle_max_spread_pct))

        self.history_file = str(history_file or "")
        self._history: Dict[str, Deque[Tuple[float, float]]] = defaultdict(self._new_history)
        self._hits: Dict[str, int] = {}
        self._first_seen: Dict[str, float] = {}
        self.last_assessments: Dict[str, TrendAssessment] = {}
        self.last_stats: Dict[str, Any] = {}
        self.history_store = ScanHistoryStore(history_file, max_scans=max(self.history_len, self.trend_lookback_scans)) if history_file else None
        if self.history_store:
            for symbol, points in self.history_store.snapshot().items():
                for point in points:
                    self._history[symbol].append((float(point.get("timestamp", 0.0)), float(point["price"])))

    @staticmethod
    def _normalise_symbols(values: Optional[Iterable[str]]) -> set[str]:
        if values is None or isinstance(values, (str, bytes)):
            values = [values] if values else []
        return {str(value).strip().upper() for value in values if str(value).strip()}

    def _new_history(self):
        return deque(maxlen=self.history_len)

    def configure(self, **kwargs):
        """Apply runtime settings without clearing accumulated scan history."""
        int_fields = {
            "movement_lookback_scans", "trend_lookback_scans", "min_confirm_scans",
            "min_consecutive_positive_scans", "history_len", "scan_interval_seconds",
            "cooldown_minutes",
        }
        bool_fields = {"btc_dump_exception_enabled", "raw_scan_trend_enabled", "require_trend_structure"}
        for key, value in kwargs.items():
            if value is None:
                continue
            if key == "history_file":
                requested = str(value or "")
                if requested != self.history_file:
                    self.history_file = requested
                    self.history_store = ScanHistoryStore(
                        requested,
                        max_scans=max(self.history_len, self.trend_lookback_scans),
                    ) if requested else None
                    if self.history_store:
                        for symbol, points in self.history_store.snapshot().items():
                            self._history[symbol].clear()
                            for point in points:
                                self._history[symbol].append(
                                    (float(point.get("timestamp", 0.0)), float(point["price"]))
                                )
                continue
            if key in {"symbol_whitelist", "symbol_blacklist"}:
                setattr(self, key, self._normalise_symbols(value))
                continue
            if not hasattr(self, key):
                continue
            try:
                if key in bool_fields:
                    setattr(self, key, bool(value))
                elif key in int_fields:
                    setattr(self, key, max(1, int(value)))
                else:
                    setattr(self, key, type(getattr(self, key))(value))
            except (TypeError, ValueError):
                continue
        self.threshold_percent = max(0.0, float(getattr(self, "threshold_percent", self.min_observed_move_pct)))
        self.min_observed_move_pct = self.threshold_percent
        self.trend_lookback_scans = max(4, int(getattr(self, "trend_lookback_scans", self.movement_lookback_scans)))
        self.movement_lookback_scans = self.trend_lookback_scans
        self.min_consecutive_positive_scans = max(1, int(getattr(self, "min_consecutive_positive_scans", 1)))

    def stats_line(self):
        """Return a human-readable per-cycle gate breakdown for the operator."""
        s = self.last_stats or {}
        return (
            "markets={local} passed={passed} | "
            "blocked(volume<{min_vol:.0f})={volume} "
            "spread>{max_spread:.2f}%={spread} "
            "trend_not_confirmed={no_trend} "
            "negative={negative} "
            "fall>{max_fall:.2f}%={falling} "
            "chase>{max_chase:.2f}%={chase} "
            "confirm={confirm} "
            "btc_dump={btc_dump} "
            "depth={depth} missing_depth={missing_depth} "
            "best_move={best_obs:.2f}%"
        ).format(
            local=s.get("local", 0), passed=s.get("passed", 0), min_vol=self.min_volume_irt,
            volume=s.get("volume", 0), max_spread=self.max_spread_pct, spread=s.get("spread", 0),
            no_trend=s.get("no_trend", 0), negative=s.get("negative", 0),
            max_fall=self.max_local_fall_pct, falling=s.get("falling", 0),
            max_chase=self.max_chase_pct, chase=s.get("chase", 0), confirm=s.get("confirm", 0),
            btc_dump=s.get("btc_dump", 0), depth=s.get("depth", 0),
            missing_depth=s.get("missing_depth", 0), best_obs=float(s.get("best_obs", 0.0) or 0.0),
        )

    @staticmethod
    def _depth(levels, n=5):
        total = 0.0
        if not isinstance(levels, list):
            return 0.0
        for level in levels[:n]:
            try:
                if isinstance(level, (list, tuple)) and len(level) >= 2:
                    total += float(level[0]) * float(level[1])
                elif isinstance(level, dict):
                    total += float(level.get("price") or level.get("p") or 0) * float(
                        level.get("amount") or level.get("quantity") or level.get("q") or 0
                    )
            except (TypeError, ValueError):
                pass
        return max(0.0, total)

    def _lookback(self, symbol, price, scans):
        history = self._history[symbol]
        if price <= 0 or not history:
            return None
        if self.require_trend_structure and len(history) < int(scans):
            return None
        # Legacy callers historically used the first observed tick until a
        # full lookback was available; strict/raw mode never does that.
        baseline = history[-int(scans)][1] if len(history) >= int(scans) else history[0][1]
        return _pct_change(price, baseline)

    def _recent(self, symbol, price):
        history = self._history[symbol]
        if price <= 0 or len(history) < 1:
            return None
        return _pct_change(price, history[-1][1])

    def _record(self, symbol, price, now, *, persist: bool = True):
        if price <= 0:
            return
        self._history[symbol].append((now, price))
        if persist and self.history_store:
            self.history_store.record(symbol, price, timestamp=now)

    def _btc_dumping(self, rows):
        for row in rows or []:
            if str(row.get("Symbol") or "").upper() != "BTC":
                continue
            ch24 = safe_float(row.get("24h Change (%)")) or 0.0
            price = safe_float(row.get("Ask")) or safe_float(row.get("Price")) or 0.0
            move = self._lookback("BTC", price, self.trend_lookback_scans)
            return min(ch24, move if move is not None else ch24) <= -self.btc_max_dump_pct
        return False

    def _eagle(self, observed, *args):
        """Evaluate the legacy BTC-dump exception argument shapes.

        Older callers passed ``recent_tick, one_hour_change, volume, spread,
        chase, local_24h``.  The current engine no longer needs the local tick
        here, but accepting both forms keeps the public compatibility method
        stable and, importantly, still uses the explicit one-hour field.
        """
        if len(args) == 5:
            one_hour_change, volume, spread, chase, local_24h = args
        elif len(args) == 6:
            _recent_tick, one_hour_change, volume, spread, chase, local_24h = args
        else:
            raise TypeError("_eagle expects 6 or 7 positional values including observed")
        if not self.btc_dump_exception_enabled or observed is None:
            return False
        one_hour = one_hour_change if one_hour_change is not None else 0.0
        return (
            observed >= self.eagle_min_observed_move_pct
            and one_hour >= self.eagle_min_1h_pct
            and volume >= self.eagle_min_volume_irt
            and spread <= self.eagle_max_spread_pct
            and chase <= self.max_chase_pct
            and (self.max_local_24h_pct <= 0 or local_24h <= self.max_local_24h_pct)
        )

    def _allowed_symbol(self, symbol: str) -> bool:
        if symbol in self.symbol_blacklist:
            return False
        return not self.symbol_whitelist or symbol in self.symbol_whitelist

    def _strict_assessment(self, symbol: str) -> TrendAssessment:
        prices = [price for _, price in self._history[symbol]]
        assessment = assess_trend(
            prices,
            threshold_percent=self.threshold_percent,
            min_consecutive_positive_scans=self.min_consecutive_positive_scans,
            trend_lookback_scans=self.trend_lookback_scans,
        )
        self.last_assessments[symbol] = assessment
        return assessment

    def _strict_candidate(
        self,
        source: Dict[str, Any],
        symbol: str,
        price: float,
        assessment: TrendAssessment,
        spread: float,
        recent: float,
        volume: float,
        now: float,
        eagle: bool = False,
    ) -> Dict[str, Any]:
        first = self._first_seen.setdefault(symbol, now)
        score = float(getattr(assessment, "ranking_score", 0.0) or 0.0)
        if score <= 0.0:
            score = max(0.0, min(100.0, assessment.cumulative_change_pct * 10.0))
        day_high = safe_float(source.get("Day High")) or 0.0
        day_low = safe_float(source.get("Day Low")) or 0.0
        day_range_pct = (
            (day_high - day_low) / day_low * 100.0
            if day_low > 0 and day_high >= day_low else 0.0
        )
        day_range_position = (
            (price - day_low) / (day_high - day_low) * 100.0
            if day_high > day_low and price > 0 else 0.0
        )
        result = dict(source)
        result.update(
            {
                "Pair": source.get("Pair") or f"{symbol}IRT",
                "AssetKey": source.get("AssetKey") or f"nobitex:{symbol.lower()}",
                "Signal": "Trend Buy",
                "signal": "Trend Buy",
                "pump_pct": assessment.cumulative_change_pct,
                "ObservedLocalMove (%)": assessment.cumulative_change_pct,
                "LocalMomentum (%)": assessment.cumulative_change_pct,
                "LocalTick (%)": recent,
                "Nobitex Spread (%)": spread,
                "Nobitex Ask": price,
                "Nobitex Bid": safe_float(source.get("Bid")) or 0.0,
                "Day Change (%)": safe_float(source.get("24h Change (%)")) or 0.0,
                "Day Open": safe_float(source.get("Day Open")) or 0.0,
                "Day High": day_high,
                "Day Low": day_low,
                "Day Range (%)": day_range_pct,
                "Day Range Position (%)": day_range_position,
                "MomentumScore": score,
                "Score": score,
                "TrendHold (sec)": max(0.0, now - first),
                "TrendConfirmScans": assessment.positive_streak,
                "ConsecutivePositiveScans": assessment.positive_streak,
                "TrendConfirmed": True,
                "trend_confirmed": True,
                "TrendBreak": bool(assessment.trend_break),
                "trend_break": bool(assessment.trend_break),
                "HigherHighs": assessment.higher_highs,
                "HigherLows": assessment.higher_lows,
                "CurrentAbovePreviousMean": assessment.current_above_previous_mean,
                "PreviousMeanRising": assessment.previous_mean_rising,
                "PreviousScanMean": assessment.previous_mean,
                "ReferencePrice": assessment.reference_price,
                "threshold_percent": self.threshold_percent,
                "min_consecutive_positive_scans": self.min_consecutive_positive_scans,
                "trend_lookback_scans": self.trend_lookback_scans,
                "StopLossPrice": price * (1.0 - self.stop_loss_percent / 100.0) if self.stop_loss_percent > 0 else 0.0,
                "stop_loss_percent": self.stop_loss_percent,
                "TrailingStopPercent": self.trailing_stop_percent,
                "trailing_stop_percent": self.trailing_stop_percent,
                "StructureScore": float(getattr(assessment, "structure_score", 0.0) or 0.0),
                "structure_score": float(getattr(assessment, "structure_score", 0.0) or 0.0),
                "EagleException": eagle,
                "DataSource": "Nobitex",
                "ExecutionVenue": "Nobitex",
            }
        )
        result["scan_history"] = [p for _, p in self._history[symbol]]
        return result

    def evaluate(self, local_rows, *, now=None, require_depth=True):
        now = time.time() if now is None else float(now)
        stats = {
            "local": 0, "passed": 0, "volume": 0, "spread": 0, "depth": 0,
            "no_trend": 0, "negative": 0, "falling": 0, "chase": 0, "confirm": 0,
            "btc_dump": 0, "btc_dump_exc": 0, "best_obs": 0.0, "invalid_quote": 0,
            "missing_depth": 0, "depth_checked": 0, "blacklist": 0, "whitelist": 0,
            "day_range_max": 0.0, "day_range_position": 0.0,
        }
        rows = list(local_rows or [])
        # Raw mode has no exchange-day/BTC exception gate; only the scanner's
        # per-symbol price histories are eligible to influence entries.
        btc_dumping = False if self.require_trend_structure else self._btc_dumping(rows)
        candidates: List[Dict[str, Any]] = []
        live_symbols = set()
        persisted_points: List[Dict[str, Any]] = []

        for source in rows:
            if not isinstance(source, dict):
                continue
            symbol = str(source.get("Symbol") or source.get("symbol") or "").upper().strip()
            if not symbol or symbol in STABLES:
                continue
            stats["local"] += 1
            live_symbols.add(symbol)
            if symbol in self.symbol_blacklist:
                stats["blacklist"] += 1
                continue
            if self.symbol_whitelist and symbol not in self.symbol_whitelist:
                stats["whitelist"] += 1
                continue

            ask = safe_float(source.get("Ask")) or 0.0
            bid = safe_float(source.get("Bid")) or 0.0
            price = ask or safe_float(source.get("Price")) or 0.0
            volume = safe_float(source.get("Volume")) or 0.0
            local_24h = safe_float(source.get("24h Change (%)")) or 0.0
            day_open = safe_float(source.get("Day Open")) or 0.0
            day_high = safe_float(source.get("Day High")) or 0.0
            day_low = safe_float(source.get("Day Low")) or 0.0
            day_range_pct = (
                (day_high - day_low) / day_low * 100.0
                if day_low > 0 and day_high >= day_low else 0.0
            )
            day_range_position = (
                (price - day_low) / (day_high - day_low) * 100.0
                if day_high > day_low and price > 0 else 0.0
            )
            stats["day_range_max"] = max(stats["day_range_max"], day_range_pct)
            if day_range_pct > 0:
                stats["day_range_position"] = max(stats["day_range_position"], day_range_position)
            one_hour_change = safe_float(source.get("1h Change (%)"))
            if price <= 0:
                stats["invalid_quote"] += 1
                continue

            recent = self._recent(symbol, price) or 0.0
            self._record(symbol, price, now, persist=False)
            persisted_points.append({"Symbol": symbol, "Price": price})
            assessment = self._strict_assessment(symbol) if self.require_trend_structure else None
            observed = assessment.cumulative_change_pct if assessment and assessment.sufficient_history else self._lookback(symbol, price, self.movement_lookback_scans)
            if observed is not None:
                stats["best_obs"] = max(stats["best_obs"], observed)

            if self.require_trend_structure:
                if not assessment or not assessment.sufficient_history or not assessment.entry_allowed:
                    stats["no_trend"] += 1
                    if assessment and assessment.is_negative:
                        stats["negative"] += 1
                    continue
            else:
                if observed is None or observed < self.min_observed_move_pct:
                    stats["no_trend"] += 1
                    self._hits.pop(symbol, None)
                    self._first_seen.pop(symbol, None)
                    continue
                hits = self._hits.get(symbol, 0) + 1
                self._hits[symbol] = hits
                if hits < self.min_confirm_scans:
                    stats["confirm"] += 1
                    continue

            if ask <= 0 or bid <= 0 or ask < bid:
                stats["invalid_quote"] += 1
                continue
            spread = (ask - bid) / bid * 100.0
            # Strict 6.1 mode is deliberately price-history-only.  The
            # legacy liquidity/day-change gates remain available to old
            # callers, but exchange 24-hour fields, volume and depth must not
            # decide whether raw movement is a valid trend entry.
            if not self.require_trend_structure and volume < self.min_volume_irt:
                stats["volume"] += 1
                continue
            if not self.require_trend_structure and spread > self.max_spread_pct:
                stats["spread"] += 1
                continue
            if not self.require_trend_structure and local_24h < 0:
                stats["negative"] += 1
                continue
            if not self.require_trend_structure and self.max_local_24h_pct > 0 and local_24h > self.max_local_24h_pct:
                stats["falling"] += 1
                continue
            if not self.require_trend_structure and recent < -self.max_local_fall_pct:
                stats["falling"] += 1
                continue
            last = safe_float(source.get("Price")) or 0.0
            chase = (ask - last) / last * 100.0 if last > 0 and ask > last else 0.0
            if not self.require_trend_structure and chase > self.max_chase_pct:
                stats["chase"] += 1
                continue

            eagle = False
            if not self.require_trend_structure and btc_dumping and symbol not in _BTC_PROXIES:
                if not self._eagle(observed, one_hour_change, volume, spread, chase, local_24h):
                    stats["btc_dump"] += 1
                    continue
                eagle = True
                stats["btc_dump_exc"] += 1

            if require_depth and not self.require_trend_structure and self.min_ask_depth_quote > 0:
                levels = source.get("asks") or source.get("AskLevels") or []
                if not levels:
                    stats["missing_depth"] += 1
                    stats["depth"] += 1
                    continue
                depth = self._depth(levels)
                stats["depth_checked"] += 1
                if depth < self.min_ask_depth_quote:
                    stats["depth"] += 1
                    continue

            if self.require_trend_structure:
                candidate = self._strict_candidate(source, symbol, price, assessment, spread, recent, volume, now, eagle)
            else:
                # Legacy output is intentionally compatible with v6 tests.
                candidate = dict(source)
                candidate.update(
                    {
                        "Pair": source.get("Pair") or f"{symbol}IRT",
                        "AssetKey": source.get("AssetKey") or f"nobitex:{symbol.lower()}",
                        "Signal": f"{'Eagle Buy' if eagle else 'Momentum Buy'} {observed:+.2f}%",
                        "pump_pct": observed,
                        "ObservedLocalMove (%)": observed,
                        "LocalMomentum (%)": observed,
                        "LocalTick (%)": recent,
                        "Nobitex Spread (%)": spread,
                        "Nobitex Ask": ask,
                        "Nobitex Bid": bid,
                        "Day Change (%)": local_24h,
                        "Day Open": day_open,
                        "Day High": day_high,
                        "Day Low": day_low,
                        "Day Range (%)": day_range_pct,
                        "Day Range Position (%)": day_range_position,
                        "MomentumScore": max(0.0, min(100.0, observed * 10.0)),
                        "Score": max(0.0, min(100.0, observed * 10.0)),
                        "TrendConfirmScans": self._hits.get(symbol, self.min_confirm_scans),
                        "EagleException": eagle,
                        "DataSource": "Nobitex",
                        "ExecutionVenue": "Nobitex",
                    }
                )
            candidates.append(candidate)
            stats["passed"] += 1

        if self.history_store and persisted_points:
            # Persist one atomic JSON snapshot per scanner cycle rather than
            # rewriting the file once per market.
            self.history_store.record_scan(persisted_points, timestamp=now)

        for symbol in list(self._hits):
            if symbol not in live_symbols:
                self._hits.pop(symbol, None)
                self._first_seen.pop(symbol, None)
        self.last_stats = stats
        candidates.sort(key=lambda row: float(row.get("MomentumScore", row.get("Score", 0.0)) or 0.0), reverse=True)
        return candidates


__all__ = ["NobitexMomentumEngine"]
