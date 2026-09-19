"""Market regime detector for adaptive aggression (v2.2).

Reads multi-timeframe signals from Nobitex market stats and picks
AGGRESSIVE, BALANCED, CONSERVATIVE, or CRISIS.

Signals used (all computed from the local book, no external API):

1.  Breadth 24h        — % of coins green over 24h
2.  Breadth short      — % of coins above their N-scan-ago price
3.  BTC 24h change     — trend reference
4.  BTC short momentum — BTC's own move over the last few scans
5.  BTC above its mean — BTC vs its rolling average
6.  Avg short momentum — average short-term move of the top 20 coins
7.  Volatility         — mean absolute return over recent scans
8.  Volume trend       — recent total volume vs earlier total volume
9.  Spread health      — median bid/ask spread across coins
10. Day-change skew    — % of coins with dayChange > 2%
11. Top-mover strength — average move of the top gainers
12. Win rate / DD      — from the tracker

v2.2 CHANGES
- FIX `_trade_stats()` to use the ACTUAL SignalTracker API.
  Previous versions called `tracker.get_closed_trades()`, which does
  NOT exist on SignalTracker — the method is `get_summary_stats()`.
  Because of the `hasattr()` guard, the helper silently fell through
  to the default dict every time:

      {"win_rate": 0.5, "trade_count": 0.0, "drawdown_pct": 0.0}

  Consequence: the `w_win_rate` weight (6 % of the score) was locked
  at 0.5 forever, and drawdown never influenced regime scoring.
  The detector therefore never learned "we've been losing lately,
  be more conservative".  The fix reads the real values from
  `get_summary_stats()` (keys: `win_rate` (0–100), `closed_trades`,
  `drawdown_pct`) and normalizes them for the scoring formula.

v2.1 additions (retained):
- strong_movers_count / strong_movers_top / top_mover_move:
  independent coins making exceptional short-term moves even when
  BTC is dumping. The engine uses this to allow "eagle" entries
  without loosening the general CONSERVATIVE/CRISIS guards.
- Presets relaxed: btc_max_dump_pct raised ~2x so a normal BTC dip
  does not freeze every entry. Real crashes still block.
- Removed unused helpers (_std, _percentile).

Two-stage confirmation: a regime flip requires N consecutive scans
agreeing on the new label. This prevents single-spike whipsaws.
"""
from __future__ import annotations

import logging
import math
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from core.utils import safe_float

logger = logging.getLogger(__name__)


AGGRESSIVE = "AGGRESSIVE"
BALANCED = "BALANCED"
CONSERVATIVE = "CONSERVATIVE"
CRISIS = "CRISIS"


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────

def _pct_change(new: float, old: float) -> Optional[float]:
    if old is None or new is None or old <= 0:
        return None
    return (float(new) - float(old)) / float(old) * 100.0


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2.0
    return s[mid]


# ────────────────────────────────────────────────────────────
# Detector
# ────────────────────────────────────────────────────────────

class RegimeDetector:
    """Multi-signal regime detector with 2-stage confirmation."""

    def __init__(
        self,
        *,
        # Score thresholds
        aggressive_score: float = 65.0,
        conservative_score: float = 35.0,
        crisis_score: float = 15.0,
        # Breadth thresholds (fractions)
        breadth_aggressive: float = 0.55,
        breadth_conservative: float = 0.35,
        # Hysteresis on score
        hysteresis: float = 5.0,
        # Two-stage confirmation
        confirm_scans: int = 2,
        # History depth (scans)
        history_len: int = 60,
        # Short-term lookback in scans
        short_lookback: int = 6,
        # Weights (relative)
        weight_breadth_24h: float = 18.0,
        weight_breadth_short: float = 12.0,
        weight_btc_24h: float = 14.0,
        weight_btc_short: float = 8.0,
        weight_btc_above_mean: float = 6.0,
        weight_avg_momentum: float = 10.0,
        weight_volume_trend: float = 6.0,
        weight_spread_health: float = 6.0,
        weight_skew: float = 8.0,
        weight_top_strength: float = 6.0,
        weight_win_rate: float = 6.0,
        min_trade_samples: int = 5,
        # Strong-mover thresholds (used by the engine's eagle exception)
        strong_mover_min_move_pct: float = 3.5,
        strong_mover_min_volume_irt: float = 200_000_000.0,
        strong_mover_max_spread_pct: float = 1.5,
        strong_mover_top_n: int = 5,
    ):
        # Thresholds
        self.aggressive_score = float(aggressive_score)
        self.conservative_score = float(conservative_score)
        self.crisis_score = float(crisis_score)
        self.breadth_aggressive = float(breadth_aggressive)
        self.breadth_conservative = float(breadth_conservative)
        self.hysteresis = float(hysteresis)
        self.confirm_scans = max(1, int(confirm_scans))
        self.history_len = max(10, int(history_len))
        self.short_lookback = max(2, int(short_lookback))
        self.min_trade_samples = int(min_trade_samples)

        # Strong-mover thresholds
        self.strong_mover_min_move_pct = float(strong_mover_min_move_pct)
        self.strong_mover_min_volume_irt = float(strong_mover_min_volume_irt)
        self.strong_mover_max_spread_pct = float(strong_mover_max_spread_pct)
        self.strong_mover_top_n = max(1, int(strong_mover_top_n))

        # Weights
        self.w_breadth_24h = float(weight_breadth_24h)
        self.w_breadth_short = float(weight_breadth_short)
        self.w_btc_24h = float(weight_btc_24h)
        self.w_btc_short = float(weight_btc_short)
        self.w_btc_above_mean = float(weight_btc_above_mean)
        self.w_avg_momentum = float(weight_avg_momentum)
        self.w_volume_trend = float(weight_volume_trend)
        self.w_spread_health = float(weight_spread_health)
        self.w_skew = float(weight_skew)
        self.w_top_strength = float(weight_top_strength)
        self.w_win_rate = float(weight_win_rate)

        # State
        self.current_regime = BALANCED
        self.last_score = 50.0
        self.last_reason = "init"
        self._pending_regime: Optional[str] = None
        self._pending_count = 0

        # History (price and volume per symbol)
        self._price_history: Dict[str, Deque[Tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=self.history_len)
        )
        self._volume_history: Deque[Tuple[float, float]] = deque(maxlen=self.history_len)
        self._btc_history: Deque[Tuple[float, float]] = deque(maxlen=self.history_len)

    # ────────────────────────────────────────────────────
    # Signal extraction
    # ────────────────────────────────────────────────────

    def _record_prices(self, rows: List[Dict[str, Any]], now: float) -> None:
        for r in rows:
            sym = str(r.get("Symbol") or "").upper()
            if not sym:
                continue
            p = safe_float(r.get("Price")) or 0.0
            if p > 0:
                self._price_history[sym].append((now, p))
        # BTC specific series
        if "BTC" in self._price_history:
            hist = self._price_history["BTC"]
            if hist:
                self._btc_history.append(hist[-1])

    def _total_volume(self, rows: List[Dict[str, Any]]) -> float:
        total = 0.0
        for r in rows:
            total += safe_float(r.get("Volume")) or 0.0
        return total

    def _breadth_24h(self, rows: List[Dict[str, Any]]) -> float:
        green = 0
        total = 0
        for r in rows:
            ch = safe_float(r.get("24h Change (%)"))
            if ch is None:
                continue
            total += 1
            if ch > 0:
                green += 1
        return green / total if total else 0.0

    def _breadth_short(self, rows: List[Dict[str, Any]]) -> float:
        if not rows:
            return 0.0
        lookback = self.short_lookback
        rising = 0
        total = 0
        for r in rows:
            sym = str(r.get("Symbol") or "").upper()
            hist = self._price_history.get(sym)
            if not hist or len(hist) < lookback:
                continue
            p_now = hist[-1][1]
            p_old = hist[-lookback][1]
            if p_old <= 0:
                continue
            total += 1
            if p_now > p_old:
                rising += 1
        return rising / total if total else 0.0

    def _btc_24h(self, rows: List[Dict[str, Any]]) -> float:
        for r in rows or []:
            if str(r.get("Symbol") or "").upper() == "BTC":
                return safe_float(r.get("24h Change (%)")) or 0.0
        return 0.0

    def _btc_short_momentum(self) -> float:
        if len(self._btc_history) < self.short_lookback:
            return 0.0
        now_p = self._btc_history[-1][1]
        old_p = self._btc_history[-self.short_lookback][1]
        return _pct_change(now_p, old_p) or 0.0

    def _btc_above_mean(self) -> float:
        if len(self._btc_history) < 10:
            return 0.0
        prices = [p for _, p in self._btc_history]
        mean = sum(prices) / len(prices)
        if mean <= 0:
            return 0.0
        return (prices[-1] - mean) / mean * 100.0

    def _avg_short_momentum(self, rows: List[Dict[str, Any]], top_n: int = 20) -> float:
        moves = []
        lookback = self.short_lookback
        for r in rows:
            sym = str(r.get("Symbol") or "").upper()
            hist = self._price_history.get(sym)
            if not hist or len(hist) < lookback:
                continue
            p_now = hist[-1][1]
            p_old = hist[-lookback][1]
            mv = _pct_change(p_now, p_old)
            if mv is not None:
                moves.append(mv)
        if not moves:
            return 0.0
        # Only top movers matter for the "is there a move" question
        moves.sort(reverse=True)
        top = moves[:top_n]
        return sum(top) / len(top) if top else 0.0

    def _volatility(self, rows: List[Dict[str, Any]]) -> float:
        """Mean absolute return over the last few scans (in %)."""
        lookback = self.short_lookback
        moves = []
        for r in rows:
            sym = str(r.get("Symbol") or "").upper()
            hist = self._price_history.get(sym)
            if not hist or len(hist) < lookback:
                continue
            p_now = hist[-1][1]
            p_old = hist[-lookback][1]
            mv = _pct_change(p_now, p_old)
            if mv is not None:
                moves.append(abs(mv))
        return sum(moves) / len(moves) if moves else 0.0

    def _volume_trend(self, rows: List[Dict[str, Any]], now: float) -> float:
        """Recent volume vs earlier average (-100..+inf, in %)."""
        total_now = self._total_volume(rows)
        if total_now <= 0:
            return 0.0
        self._volume_history.append((now, total_now))
        if len(self._volume_history) < 6:
            return 0.0
        recent = [v for _, v in list(self._volume_history)[-3:]]
        earlier = [v for _, v in list(self._volume_history)[-6:-3]]
        if not earlier:
            return 0.0
        avg_earlier = sum(earlier) / len(earlier)
        avg_recent = sum(recent) / len(recent)
        if avg_earlier <= 0:
            return 0.0
        return (avg_recent - avg_earlier) / avg_earlier * 100.0

    def _spread_health(self, rows: List[Dict[str, Any]]) -> float:
        """Median spread. Lower is healthier. Returns % (0..big)."""
        spreads = []
        for r in rows:
            bid = safe_float(r.get("Bid")) or 0.0
            ask = safe_float(r.get("Ask")) or 0.0
            if bid > 0 and ask >= bid:
                spreads.append((ask - bid) / bid * 100.0)
        return _median(spreads)

    def _day_change_skew(self, rows: List[Dict[str, Any]]) -> float:
        """Fraction of coins with 24h change > 2% (strong movers)."""
        if not rows:
            return 0.0
        strong = 0
        total = 0
        for r in rows:
            ch = safe_float(r.get("24h Change (%)"))
            if ch is None:
                continue
            total += 1
            if ch > 2.0:
                strong += 1
        return strong / total if total else 0.0

    def _top_strength(self, rows: List[Dict[str, Any]], top_n: int = 10) -> float:
        """Average 24h change of the top gainers."""
        changes = []
        for r in rows:
            ch = safe_float(r.get("24h Change (%)"))
            if ch is not None:
                changes.append(ch)
        changes.sort(reverse=True)
        top = changes[:top_n]
        return sum(top) / len(top) if top else 0.0

    # ────────────────────────────────────────────────────
    # Strong movers — coins that move against a bad tape
    # ────────────────────────────────────────────────────

    def _strong_movers(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Return (count, top_symbols, top_move) for coins making an
        exceptional short move *with* real liquidity.

        The engine uses this list to permit entries even when BTC is
        dumping — the eagle exception. Filters are intentionally strict:
        move >= strong_mover_min_move_pct, volume >= min, spread tight.
        """
        lookback = self.short_lookback
        candidates: List[Tuple[float, str, Dict[str, Any]]] = []
        for r in rows:
            sym = str(r.get("Symbol") or "").upper()
            if not sym or sym in {"USDT", "USDC", "USD", "IRT", "RLS"}:
                continue
            hist = self._price_history.get(sym)
            if not hist or len(hist) < lookback:
                continue
            p_now = hist[-1][1]
            p_old = hist[-lookback][1]
            mv = _pct_change(p_now, p_old)
            if mv is None or mv < self.strong_mover_min_move_pct:
                continue
            vol = safe_float(r.get("Volume")) or 0.0
            if vol < self.strong_mover_min_volume_irt:
                continue
            bid = safe_float(r.get("Bid")) or 0.0
            ask = safe_float(r.get("Ask")) or 0.0
            if bid <= 0 or ask < bid:
                continue
            spread = (ask - bid) / bid * 100.0
            if spread > self.strong_mover_max_spread_pct:
                continue
            candidates.append((mv, sym, r))

        if not candidates:
            return {"count": 0, "top_symbols": [], "top_move": 0.0}

        candidates.sort(key=lambda x: x[0], reverse=True)
        top = candidates[: self.strong_mover_top_n]
        return {
            "count": len(candidates),
            "top_symbols": [sym for _, sym, _ in top],
            "top_move": float(top[0][0]),
        }

    # FIX v2.2: read from the real SignalTracker API.
    def _trade_stats(self, tracker: Any) -> Dict[str, float]:
        """
        Extract win-rate / trade-count / drawdown from a SignalTracker.

        FIX v2.2:
            The pre-fix version called `tracker.get_closed_trades()`,
            which does NOT exist on SignalTracker.  The `hasattr` guard
            made the helper silently fall through to defaults:

                {"win_rate": 0.5, "trade_count": 0.0, "drawdown_pct": 0.0}

            Consequence: the `w_win_rate` weight (6 % of the total
            score) was locked at 0.5 forever and drawdown never
            influenced the regime decision — the detector never
            learned "we've been losing lately, be more conservative".

            The fix reads real values from `get_summary_stats()`,
            which IS the canonical SignalTracker API.  Its keys are:

                win_rate      -> 0..100  (percent)
                closed_trades -> int
                drawdown_pct  -> 0..100  (percent)

            We normalize win_rate to 0..1 to match the scoring helper's
            expected range.  If the tracker API is missing entirely or
            raises, we fall back to the neutral defaults (0.5 / 0 / 0),
            which is the same behaviour as before — safe, just not
            informative.
        """
        out = {"win_rate": 0.5, "trade_count": 0.0, "drawdown_pct": 0.0}
        if tracker is None:
            return out

        # Preferred path: SignalTracker.get_summary_stats()
        get_summary = getattr(tracker, "get_summary_stats", None)
        if callable(get_summary):
            try:
                summary = get_summary() or {}
                if isinstance(summary, dict):
                    closed = summary.get("closed_trades")
                    if closed is None:
                        closed = summary.get("total_closed")
                    try:
                        out["trade_count"] = float(closed or 0)
                    except (TypeError, ValueError):
                        out["trade_count"] = 0.0

                    wr_pct = summary.get("win_rate")
                    wr = safe_float(wr_pct)
                    if wr is not None:
                        # SignalTracker reports 0..100; normalise to 0..1.
                        out["win_rate"] = max(0.0, min(1.0, float(wr) / 100.0))

                    dd = safe_float(summary.get("drawdown_pct"))
                    if dd is not None:
                        out["drawdown_pct"] = max(0.0, float(dd))

                    return out
            except Exception as exc:
                logger.debug("regime_detector._trade_stats: summary failed: %s", exc)

        # Fallback path: try a trade-list method if the tracker has one.
        # (Kept for compatibility with alternative tracker implementations
        # that might expose `get_enriched_trades`.)
        trades: List[Dict[str, Any]] = []
        for method_name in ("get_enriched_trades", "get_all_trades"):
            fn = getattr(tracker, method_name, None)
            if not callable(fn):
                continue
            try:
                raw = fn() or []
                if isinstance(raw, list):
                    trades = [t for t in raw if isinstance(t, dict) and t.get("status") == "closed"]
                    if trades:
                        break
            except Exception as exc:
                logger.debug(
                    "regime_detector._trade_stats: %s failed: %s",
                    method_name, exc,
                )

        if trades:
            recent = trades[-20:]
            wins = 0
            for t in recent:
                pnl = safe_float(
                    t.get("pnl_pct_net")
                    or t.get("pnl_pct")
                    or t.get("pnl_percent")
                    or t.get("profit_pct")
                    or 0.0
                ) or 0.0
                if pnl > 0:
                    wins += 1
            out["trade_count"] = float(len(recent))
            out["win_rate"] = wins / len(recent) if recent else 0.5

        # Drawdown fallback from tracker attributes.
        try:
            peak = float(getattr(tracker, "peak_equity", 0.0) or 0.0)
            equity = float(
                getattr(tracker, "account_balance", None)
                or getattr(tracker, "cash", 0.0)
                or 0.0
            )
            if peak > 0 and equity >= 0:
                out["drawdown_pct"] = max(0.0, (peak - equity) / peak * 100.0)
        except Exception:
            pass

        return out

    # ────────────────────────────────────────────────────
    # Scoring
    # ────────────────────────────────────────────────────

    @staticmethod
    def _clamp(x: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, x))

    @staticmethod
    def _scale(value: float, lo: float, hi: float) -> float:
        """Map value from [lo..hi] to [0..1]."""
        if hi <= lo:
            return 0.5
        return RegimeDetector._clamp((value - lo) / (hi - lo), 0.0, 1.0)

    def score(
        self,
        local_rows: List[Dict[str, Any]],
        tracker: Any = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Compute a 0..100 health score with all sub-signals."""
        now = time.time() if now is None else float(now)

        # Record price history before computing short-term signals
        self._record_prices(local_rows, now)

        # Signals
        breadth_24h = self._breadth_24h(local_rows)
        breadth_short = self._breadth_short(local_rows)
        btc_24h = self._btc_24h(local_rows)
        btc_short = self._btc_short_momentum()
        btc_above_mean = self._btc_above_mean()
        avg_momentum = self._avg_short_momentum(local_rows)
        volatility = self._volatility(local_rows)
        volume_trend = self._volume_trend(local_rows, now)
        spread = self._spread_health(local_rows)
        skew = self._day_change_skew(local_rows)
        top_strength = self._top_strength(local_rows)
        stats = self._trade_stats(tracker)
        strong = self._strong_movers(local_rows)

        # ── Component scores (0..1) ──
        s_breadth_24h    = self._scale(breadth_24h,    0.20, 0.65)
        s_breadth_short  = self._scale(breadth_short,  0.25, 0.70)
        s_btc_24h        = self._scale(btc_24h,       -3.0,   3.0)
        s_btc_short      = self._scale(btc_short,     -1.5,   1.5)
        s_btc_above_mean = self._scale(btc_above_mean,-1.5,   1.5)
        s_avg_momentum   = self._scale(avg_momentum,  -0.5,   1.5)
        s_volume_trend   = self._scale(volume_trend, -30.0,  30.0)
        # Spread health: 0.10% tight is great, 2.0% wide is bad
        s_spread         = 1.0 - self._scale(spread,   0.10,  2.0)
        s_skew           = self._scale(skew,           0.02,  0.20)
        s_top_strength   = self._scale(top_strength,   0.5,   6.0)

        if stats["trade_count"] >= self.min_trade_samples:
            s_win_rate = self._scale(stats["win_rate"], 0.30, 0.70)
        else:
            s_win_rate = 0.5

        # ── Weighted total (sum weights = 100) ──
        total_weight = (
            self.w_breadth_24h + self.w_breadth_short + self.w_btc_24h +
            self.w_btc_short + self.w_btc_above_mean + self.w_avg_momentum +
            self.w_volume_trend + self.w_spread_health + self.w_skew +
            self.w_top_strength + self.w_win_rate
        )
        weighted = (
            s_breadth_24h   * self.w_breadth_24h +
            s_breadth_short * self.w_breadth_short +
            s_btc_24h       * self.w_btc_24h +
            s_btc_short     * self.w_btc_short +
            s_btc_above_mean* self.w_btc_above_mean +
            s_avg_momentum  * self.w_avg_momentum +
            s_volume_trend  * self.w_volume_trend +
            s_spread        * self.w_spread_health +
            s_skew          * self.w_skew +
            s_top_strength  * self.w_top_strength +
            s_win_rate      * self.w_win_rate
        )
        score = (weighted / total_weight) * 100.0 if total_weight > 0 else 50.0

        # ── Hard overrides ──
        # BTC dumping hard OR very wide spreads OR extreme volatility
        # can push the score down quickly.
        if btc_24h <= -4.0 or btc_short <= -2.5:
            score = min(score, 25.0)
        if spread >= 3.0:
            score = min(score, 30.0)
        if volatility >= 3.5:
            score = min(score, 35.0)

        # BTC pumping hard plus broad participation = aggressive
        if btc_24h >= 4.0 and breadth_24h >= 0.65:
            score = max(score, 80.0)

        # ── Strong-mover escape hatch ──
        # If the market is otherwise bad but a handful of coins are
        # making real, liquid, tight-spread moves, lift the score just
        # enough to escape deep CONSERVATIVE / CRISIS lock. The engine
        # still has to approve each entry on its own merits.
        if strong["count"] >= 3 and score < self.conservative_score:
            score = min(score + 10.0, self.conservative_score + 4.0)
        if strong["count"] >= 6 and score < self.aggressive_score:
            score = min(score + 5.0, self.aggressive_score - 2.0)

        score = self._clamp(score, 0.0, 100.0)
        self.last_score = score

        return {
            "score": score,
            "breadth": breadth_24h,           # keep name for compat
            "breadth_24h": breadth_24h,
            "breadth_short": breadth_short,
            "btc_24h": btc_24h,
            "btc_short": btc_short,
            "btc_above_mean": btc_above_mean,
            "avg_momentum": avg_momentum,
            "volatility": volatility,
            "volume_trend": volume_trend,
            "spread": spread,
            "skew": skew,
            "top_strength": top_strength,
            "win_rate": stats["win_rate"],
            "trade_count": int(stats["trade_count"]),
            "drawdown_pct": stats["drawdown_pct"],
            # Strong-mover signals — engine uses these for the eagle exception
            "strong_movers_count": strong["count"],
            "strong_movers_top": strong["top_symbols"],
            "top_mover_move": strong["top_move"],
            "components": {
                "breadth_24h":   round(s_breadth_24h * self.w_breadth_24h, 1),
                "breadth_short": round(s_breadth_short * self.w_breadth_short, 1),
                "btc_24h":       round(s_btc_24h * self.w_btc_24h, 1),
                "btc_short":     round(s_btc_short * self.w_btc_short, 1),
                "btc_above":     round(s_btc_above_mean * self.w_btc_above_mean, 1),
                "momentum":      round(s_avg_momentum * self.w_avg_momentum, 1),
                "volume":        round(s_volume_trend * self.w_volume_trend, 1),
                "spread":        round(s_spread * self.w_spread_health, 1),
                "skew":          round(s_skew * self.w_skew, 1),
                "top":           round(s_top_strength * self.w_top_strength, 1),
                "win_rate":      round(s_win_rate * self.w_win_rate, 1),
            },
        }

    # ────────────────────────────────────────────────────
    # Decision with 2-stage confirmation
    # ────────────────────────────────────────────────────

    def _raw_regime(self, info: Dict[str, Any]) -> str:
        score = float(info.get("score", 50.0))
        breadth = float(info.get("breadth_24h", info.get("breadth", 0.5)))
        dd = float(info.get("drawdown_pct", 0.0))
        btc_short = float(info.get("btc_short", 0.0))
        spread = float(info.get("spread", 0.0))
        strong_count = int(info.get("strong_movers_count", 0) or 0)

        # Crisis overrides — but a cluster of strong movers delays it.
        if (
            dd >= 12.0
            or spread >= 3.5
            or (btc_short <= -3.0 and strong_count < 2)
        ):
            return CRISIS
        if score <= self.crisis_score and strong_count < 2:
            return CRISIS

        if (
            score >= self.aggressive_score
            and breadth >= self.breadth_aggressive
            and btc_short >= 0
        ):
            return AGGRESSIVE

        # CONSERVATIVE only when there is nothing to buy.
        if (
            score <= self.conservative_score
            or breadth <= self.breadth_conservative
            or btc_short <= -1.5
        ):
            # If a strong cluster is present, stay BALANCED instead of
            # dropping to CONSERVATIVE — the engine's exception path
            # will still do the final per-symbol vetting.
            if strong_count >= 3:
                return BALANCED
            return CONSERVATIVE

        return BALANCED

    def decide(self, info: Dict[str, Any]) -> str:
        """Two-stage confirmation: require N consecutive scans for a flip."""
        raw = self._raw_regime(info)

        if raw == self.current_regime:
            self._pending_regime = None
            self._pending_count = 0
            self.last_reason = f"hold {raw} (score={info.get('score', 0):.1f})"
            return self.current_regime

        # Score-based hysteresis for small step changes
        score = float(info.get("score", 50.0))
        order = {CRISIS: 0, CONSERVATIVE: 1, BALANCED: 2, AGGRESSIVE: 3}
        step = abs(order.get(raw, 2) - order.get(self.current_regime, 2))
        if step == 1:
            if self.current_regime == BALANCED and raw == AGGRESSIVE:
                if score < self.aggressive_score + self.hysteresis:
                    self.last_reason = f"blocked AGG (score={score:.1f})"
                    return self.current_regime
            elif self.current_regime == AGGRESSIVE and raw == BALANCED:
                if score > self.aggressive_score - self.hysteresis:
                    self.last_reason = f"hold AGG (score={score:.1f})"
                    return self.current_regime
            elif self.current_regime == BALANCED and raw == CONSERVATIVE:
                if score > self.conservative_score - self.hysteresis:
                    self.last_reason = f"blocked CONS (score={score:.1f})"
                    return self.current_regime
            elif self.current_regime == CONSERVATIVE and raw == BALANCED:
                if score < self.conservative_score + self.hysteresis:
                    self.last_reason = f"hold CONS (score={score:.1f})"
                    return self.current_regime

        # Direct crisis / direct recovery skip the confirmation queue
        if raw == CRISIS or self.current_regime == CRISIS:
            self.current_regime = raw
            self._pending_regime = None
            self._pending_count = 0
            self.last_reason = f"direct {raw} (score={score:.1f})"
            return raw

        # Count confirmations
        if self._pending_regime == raw:
            self._pending_count += 1
        else:
            self._pending_regime = raw
            self._pending_count = 1

        if self._pending_count >= self.confirm_scans:
            prev = self.current_regime
            self.current_regime = raw
            self._pending_regime = None
            self._pending_count = 0
            self.last_reason = f"{prev} -> {raw} (score={score:.1f})"
            return raw

        self.last_reason = (
            f"pending {raw} ({self._pending_count}/{self.confirm_scans}, "
            f"score={score:.1f})"
        )
        return self.current_regime


# ────────────────────────────────────────────────────────────
# Parameter presets per regime
# ────────────────────────────────────────────────────────────

REGIME_PRESETS = {
    AGGRESSIVE: {
        "pump_threshold_pct": 1.2,
        "min_observed_move_pct": 0.5,
        "movement_lookback_scans": 4,
        "min_confirm_scans": 1,
        "max_chase_pct": 1.0,
        "max_spread_pct": 1.5,
        "min_volume_24h": 200_000_000.0,
        "max_local_24h_pct": 20.0,
        "max_local_fall_pct": 1.2,
        "btc_max_dump_pct": 2.5,
        "max_new_entries_per_cycle": 2,
        "fixed_position_quote": 4_000_000.0,
        "max_open_positions": 10,
        "stop_loss_pct": 3.5,
        "trailing_distance_pct": 2.5,
    },
    BALANCED: {
        "pump_threshold_pct": 1.8,
        "min_observed_move_pct": 0.8,
        "movement_lookback_scans": 6,
        "min_confirm_scans": 1,
        "max_chase_pct": 0.8,
        "max_spread_pct": 1.2,
        "min_volume_24h": 500_000_000.0,
        "max_local_24h_pct": 15.0,
        "max_local_fall_pct": 0.8,
        "btc_max_dump_pct": 2.0,
        "max_new_entries_per_cycle": 1,
        "fixed_position_quote": 3_000_000.0,
        "max_open_positions": 10,
        "stop_loss_pct": 3.0,
        "trailing_distance_pct": 2.0,
    },
    CONSERVATIVE: {
        "pump_threshold_pct": 2.2,
        "min_observed_move_pct": 1.2,
        "movement_lookback_scans": 8,
        "min_confirm_scans": 2,
        "max_chase_pct": 0.6,
        "max_spread_pct": 0.9,
        "min_volume_24h": 800_000_000.0,
        "max_local_24h_pct": 12.0,
        "max_local_fall_pct": 0.6,
        "btc_max_dump_pct": 1.5,
        "max_new_entries_per_cycle": 1,
        "fixed_position_quote": 1_500_000.0,
        "max_open_positions": 5,
        "stop_loss_pct": 2.5,
        "trailing_distance_pct": 1.5,
    },
    CRISIS: {
        "pump_threshold_pct": 3.5,
        "min_observed_move_pct": 2.0,
        "movement_lookback_scans": 10,
        "min_confirm_scans": 3,
        "max_chase_pct": 0.3,
        "max_spread_pct": 0.6,
        "min_volume_24h": 1_500_000_000.0,
        "max_local_24h_pct": 8.0,
        "max_local_fall_pct": 0.4,
        "btc_max_dump_pct": 1.0,
        "max_new_entries_per_cycle": 0,
        "fixed_position_quote": 1_000_000.0,
        "max_open_positions": 3,
        "stop_loss_pct": 2.0,
        "trailing_distance_pct": 1.2,
    },
}