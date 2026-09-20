"""Nobitex-only IRT momentum engine.

Uses only live Nobitex IRT market data. It never places orders.
"""
from __future__ import annotations
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple
from core.utils import safe_float

STABLES = {"USDT", "USDC", "USD", "DAI", "TUSD", "FDUSD", "USDE", "PYUSD"}
_BTC_PROXIES = {"BTC", "WBTC", "TBTC"}

def _pct_change(new: float, old: float) -> Optional[float]:
    if old <= 0 or new <= 0:
        return None
    return (float(new) - float(old)) / float(old) * 100.0

class NobitexMomentumEngine:
    """Fast stateful momentum filter for the Nobitex IRT spot book."""
    def __init__(self, *, pump_threshold_pct=1.5, max_spread_pct=0.9,
                 min_volume_irt=300_000_000.0, max_local_24h_pct=15.0,
                 max_local_fall_pct=0.8, max_chase_pct=0.8,
                 movement_lookback_scans=4, min_confirm_scans=1,
                 min_observed_move_pct=0.8, min_ask_depth_quote=300_000.0,
                 history_len=60, btc_dump_exception_enabled=True,
                 btc_max_dump_pct=2.5, eagle_min_observed_move_pct=2.5,
                 eagle_min_1h_pct=2.0, eagle_min_volume_irt=300_000_000.0,
                 eagle_max_spread_pct=0.9, **_):
        self.pump_threshold_pct=max(0.1,float(pump_threshold_pct))
        self.max_spread_pct=max(0.05,float(max_spread_pct))
        self.min_volume_irt=max(0.0,float(min_volume_irt))
        self.max_local_24h_pct=float(max_local_24h_pct)
        self.max_local_fall_pct=max(0.0,float(max_local_fall_pct))
        self.max_chase_pct=max(0.0,float(max_chase_pct))
        self.movement_lookback_scans=max(1,int(movement_lookback_scans))
        self.min_confirm_scans=max(1,int(min_confirm_scans))
        self.min_observed_move_pct=max(0.0,float(min_observed_move_pct))
        self.min_ask_depth_quote=max(0.0,float(min_ask_depth_quote))
        self.history_len=max(8,int(history_len))
        self.btc_dump_exception_enabled=bool(btc_dump_exception_enabled)
        self.btc_max_dump_pct=max(0.0,float(btc_max_dump_pct))
        self.eagle_min_observed_move_pct=max(0.0,float(eagle_min_observed_move_pct))
        self.eagle_min_1h_pct=max(0.0,float(eagle_min_1h_pct))
        self.eagle_min_volume_irt=max(0.0,float(eagle_min_volume_irt))
        self.eagle_max_spread_pct=max(0.05,float(eagle_max_spread_pct))
        self._history: Dict[str,Deque[Tuple[float,float]]]=defaultdict(self._new_history)
        self._hits: Dict[str,int]={}
        self._first_seen: Dict[str,float]={}
        self.last_stats: Dict[str,Any]={}

    def _new_history(self):
        return deque(maxlen=self.history_len)

    def configure(self, **kwargs):
        int_fields={"movement_lookback_scans","min_confirm_scans","history_len"}
        bool_fields={"btc_dump_exception_enabled"}
        for key,value in kwargs.items():
            if not hasattr(self,key) or value is None:
                continue
            try:
                if key in bool_fields: setattr(self,key,bool(value))
                elif key in int_fields: setattr(self,key,max(1,int(value)))
                else: setattr(self,key,type(getattr(self,key))(value))
            except (TypeError,ValueError):
                continue

    def stats_line(self):
        """Return a human-readable per-cycle gate breakdown for the operator."""
        s = self.last_stats or {}
        return (
            "markets={local} passed={passed} | "
            "blocked(volume<{min_vol:.0f})={volume} "
            "spread>{max_spread:.2f}%={spread} "
            "move<{min_move:.2f}%={no_trend} "
            "fall>{max_fall:.2f}%={falling} "
            "chase>{max_chase:.2f}%={chase} "
            "confirm={confirm} "
            "btc_dump={btc_dump} eagle={btc_dump_exc} "
            "depth={depth} missing_depth={missing_depth} "
            "best_move={best_obs:.2f}%"
        ).format(
            local=s.get("local", 0),
            passed=s.get("passed", 0),
            min_vol=self.min_volume_irt,
            volume=s.get("volume", 0),
            max_spread=self.max_spread_pct,
            spread=s.get("spread", 0),
            min_move=self.min_observed_move_pct,
            no_trend=s.get("no_trend", 0),
            max_fall=self.max_local_fall_pct,
            falling=s.get("falling", 0),
            max_chase=self.max_chase_pct,
            chase=s.get("chase", 0),
            confirm=s.get("confirm", 0),
            btc_dump=s.get("btc_dump", 0),
            btc_dump_exc=s.get("btc_dump_exc", 0),
            depth=s.get("depth", 0),
            missing_depth=s.get("missing_depth", 0),
            best_obs=float(s.get("best_obs", 0.0) or 0.0),
        )

    @staticmethod
    def _depth(levels,n=5):
        total=0.0
        if not isinstance(levels,list): return 0.0
        for level in levels[:n]:
            try:
                if isinstance(level,(list,tuple)) and len(level)>=2:
                    total += float(level[0])*float(level[1])
                elif isinstance(level,dict):
                    total += float(level.get("price") or level.get("p") or 0)*float(
                        level.get("amount") or level.get("quantity") or level.get("q") or 0)
            except (TypeError,ValueError):
                pass
        return max(0.0,total)

    def _lookback(self,symbol,price,scans):
        h=self._history[symbol]
        if price<=0 or not h: return None
        baseline=h[-max(1,int(scans))][1] if len(h)>=int(scans) else h[0][1]
        return _pct_change(price,float(baseline))

    def _recent(self,symbol,price):
        h=self._history[symbol]
        if price<=0 or len(h)<2: return None
        return _pct_change(price,h[-2][1])

    def _record(self,symbol,price,now):
        if price>0: self._history[symbol].append((now,price))

    def _btc_dumping(self,rows):
        for row in rows or []:
            if str(row.get("Symbol") or "").upper()!="BTC": continue
            ch24=safe_float(row.get("24h Change (%)")) or 0.0
            price=safe_float(row.get("Ask")) or safe_float(row.get("Price")) or 0.0
            move=self._lookback("BTC",price,self.movement_lookback_scans)
            return min(ch24,move if move is not None else ch24) <= -self.btc_max_dump_pct
        return False

    def _eagle(
        self, observed, recent_tick, one_hour_change, volume, spread,
        chase, local_24h,
    ):
        """Allow an exceptional local mover during a BTC sell-off.

        `eagle_min_1h_pct` must be evaluated against Nobitex's actual
        1-hour market-stat change, not against the last scan-to-scan tick.
        """
        if not self.btc_dump_exception_enabled or observed is None:
            return False
        one_hour = one_hour_change if one_hour_change is not None else 0.0
        return (
            observed >= self.eagle_min_observed_move_pct
            and one_hour >= self.eagle_min_1h_pct
            and volume >= self.eagle_min_volume_irt
            and spread <= self.eagle_max_spread_pct
            and chase <= self.max_chase_pct
            and (
                self.max_local_24h_pct <= 0
                or local_24h <= self.max_local_24h_pct
            )
        )

    def evaluate(self, local_rows, *, now=None, require_depth=True):
        now=time.time() if now is None else float(now)
        stats={"local":0,"passed":0,"volume":0,"spread":0,"depth":0,"no_trend":0,
               "falling":0,"chase":0,"confirm":0,"btc_dump":0,"btc_dump_exc":0,
               "best_obs":0.0,"invalid_quote":0,"missing_depth":0,"depth_checked":0,
               "age":0,"cooldown":0,"blacklist":0,"already_open":0,"min_notional":0}
        btc_dumping=self._btc_dumping(local_rows)
        candidates=[]; live_symbols=set()
        for source in local_rows or []:
            symbol=str(source.get("Symbol") or "").upper().strip()
            if not symbol or symbol in STABLES: continue
            stats["local"]+=1; live_symbols.add(symbol)
            ask=safe_float(source.get("Ask")) or 0.0
            bid=safe_float(source.get("Bid")) or 0.0
            price=ask or safe_float(source.get("Price")) or 0.0
            volume=safe_float(source.get("Volume")) or 0.0
            local_24h=safe_float(source.get("24h Change (%)")) or 0.0
            one_hour_change=safe_float(source.get("1h Change (%)"))
            observed=self._lookback(symbol,price,self.movement_lookback_scans)
            recent=self._recent(symbol,price)
            if observed is not None:
                stats["best_obs"] = max(stats["best_obs"], observed)
            self._record(symbol,price,now)
            if ask<=0 or bid<=0 or ask<bid: continue
            spread=(ask-bid)/bid*100.0
            if volume<self.min_volume_irt: stats["volume"]+=1; continue
            if spread>self.max_spread_pct: stats["spread"]+=1; continue
            # Depth is an execution/liquidity gate, not a discovery gate.
            # It is deliberately checked after momentum filters so the scanner
            # does not request an order book for every market on every cycle.
            # The GUI/execution layer supplies `asks` for the small set of
            # momentum candidates. Missing depth is fail-closed.
            if self.max_local_24h_pct>0 and local_24h>self.max_local_24h_pct: stats["falling"]+=1; continue
            if observed is None or observed<self.min_observed_move_pct:
                stats["no_trend"]+=1; self._hits.pop(symbol,None); self._first_seen.pop(symbol,None); continue
            local_tick=recent if recent is not None else 0.0
            if local_tick< -self.max_local_fall_pct:
                stats["falling"]+=1; self._hits.pop(symbol,None); self._first_seen.pop(symbol,None); continue
            last=safe_float(source.get("Price")) or 0.0
            chase=(ask-last)/last*100.0 if last>0 and ask>last else 0.0
            if chase>self.max_chase_pct: stats["chase"]+=1; continue
            eagle=False
            if btc_dumping and symbol not in _BTC_PROXIES:
                eagle=self._eagle(
                    observed, local_tick, one_hour_change, volume,
                    spread, chase, local_24h,
                )
                if not eagle: stats["btc_dump"]+=1; continue
                stats["btc_dump_exc"]+=1
            if observed<self.pump_threshold_pct and not eagle:
                hits=self._hits.get(symbol,0)+1; self._hits[symbol]=hits
                if hits<self.min_confirm_scans: stats["confirm"]+=1; continue
            else:
                hits=max(1,self._hits.get(symbol,0)+1); self._hits[symbol]=hits
            if require_depth and self.min_ask_depth_quote > 0:
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
            first=self._first_seen.setdefault(symbol,now)
            score=(min(observed,10)*12 + min(max(local_tick,0),2)*8 +
                   min(volume/max(self.min_volume_irt,1),10)*2 +
                   min(hits,6)*2 - spread*8 - max(chase,0)*10)
            result=dict(source)
            result.update({"Pair":source.get("Pair") or f"{symbol}IRT",
                           "AssetKey":source.get("AssetKey") or f"nobitex:{symbol.lower()}",
                           "Signal":f"{'Eagle Buy' if eagle else 'Momentum Buy'} {observed:+.2f}%",
                           "pump_pct":observed,"ObservedLocalMove (%)":observed,
                           "LocalMomentum (%)":observed,"LocalTick (%)":local_tick,
                           "Nobitex Spread (%)":spread,"Nobitex Ask":ask,"Nobitex Bid":bid,
                           "MomentumScore":score,"Score":score,"TrendHold (sec)":max(0,now-first),
                           "TrendConfirmScans":hits,"EagleException":eagle,
                           "DataSource":"Nobitex","ExecutionVenue":"Nobitex"})
            candidates.append(result); stats["passed"]+=1
        for symbol in list(self._hits):
            if symbol not in live_symbols: self._hits.pop(symbol,None); self._first_seen.pop(symbol,None)
        self.last_stats=stats
        candidates.sort(key=lambda x:float(x.get("MomentumScore",0)),reverse=True)
        return candidates
