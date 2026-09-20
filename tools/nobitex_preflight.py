#!/usr/bin/env python3
"""پیش‌پرواز تست نوبیتکس (Nobitex preflight).

قبل از اجرای ربات، این اسکریپت سه چیز را بررسی می‌کند:

1. اتصال به نوبیتکس و دریافت دادهٔ واقعی بازار (`/market/stats`).
2. اسپرد واقعی هر جفت‌ارز و هزینهٔ رفت‌وبرگشت (کارمزد + اسپرد).
3. اینکه «گارد هزینه» (cost guard) با تنظیمات فعلی، ورود را رد می‌کند یا نه
   — با همان کدی که در `signal_tracker.py` اجرا می‌شود، نه یک بازنویسی دستی.

اجرا:

    python tools/nobitex_preflight.py            # آنلاین + آفلاین
    python tools/nobitex_preflight.py --offline  # فقط حساب‌وکتاب محلی
    python tools/nobitex_preflight.py --pair BTCIRT

برای دریافت دادهٔ عمومی (Bid/Ask) به کلید API نیازی نیست؛ کلید فقط برای
حالت واقعی معاملات لازم است.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from signal_tracker import SignalTracker  # noqa: E402
from trading.bot_config import DEFAULT_CONFIG_FILE, load_config  # noqa: E402

GUARD_FIELDS = (
    "trading_fee_pct",
    "paper_half_spread_pct",
    "max_nobitex_spread_pct",
    "trailing_activation_pct",
    "trailing_distance_pct",
    "min_edge_multiple",
    "stop_loss_pct",
)


def build_shadow_tracker(cfg) -> SignalTracker:
    """یک نمونهٔ سبک از SignalTracker که فقط منطق گارد هزینه را دارد."""
    st = SignalTracker.__new__(SignalTracker)
    st.mode = "real" if str(cfg.execution_mode).lower() == "real" else "paper"
    for name in GUARD_FIELDS:
        setattr(st, name, getattr(cfg, name, 0.0))
    st.cost_guard_enabled = True  # می‌خواهیم ببینیم گارد چه می‌گوید
    if str(cfg.execution_mode).lower() == "real":
        st.paper_half_spread_pct = 0.0
    return st


def local_geometry(cfg) -> Dict[str, float]:
    """حساب‌وکتاب آفلاین بر پایهٔ تنظیمات همان فایل پیکربندی."""
    st = build_shadow_tracker(cfg)
    cost = st.round_trip_cost_pct(None)
    gap = max(0.0, float(cfg.trailing_activation_pct) - float(cfg.trailing_distance_pct))
    return {
        "fee_round_trip": 2.0 * float(cfg.trading_fee_pct),
        "cost": cost,
        "trailing_gap": gap,
        "required_move": float(cfg.min_edge_multiple) * cost,
        "half_spread": float(cfg.paper_half_spread_pct),
    }


def fmt_pct(value: float) -> str:
    return f"{value:.2f}%"


def offline_report(cfg) -> None:
    geo = local_geometry(cfg)
    print("── حساب‌وکتاب هزینه (بدون نیاز به شبکه) ──")
    print(f"حالت اجرا                        : {cfg.execution_mode}")
    print(f"کارمزد رفت‌وبرگشت (۲ × {fmt_pct(float(cfg.trading_fee_pct))})   : "
          f"{fmt_pct(geo['fee_round_trip'])}")
    print(f"نیم‌اسپرد شبیه‌سازی هر طرف        : {fmt_pct(geo['half_spread'])}")
    print(f"هزینهٔ برآوردی رفت‌وبرگشت        : {fmt_pct(geo['cost'])}")
    print(f"فاصلهٔ تریلینگ                    : {fmt_pct(geo['trailing_gap'])} "
          f"(فعال‌سازی {cfg.trailing_activation_pct}% − فاصله {cfg.trailing_distance_pct}%)")
    print(f"حد ضرر                            : {cfg.stop_loss_pct}%")
    hold = int(getattr(cfg, "max_hold_minutes", 0) or 0)
    print("توقف زمانی                        : "
          + (f"{hold} دقیقه" if hold > 0
             else "خاموش (پوزیشن می‌تواند روزها باز بماند)"))
    print(f"کم‌ترین حرکت لازم برای ورود       : {fmt_pct(geo['required_move'])} "
          f"({cfg.min_edge_multiple} × هزینه)")
    print(f"آستانهٔ سیگنال مشاهده‌شده          : {cfg.min_observed_move_pct}% / "
          f"{cfg.pump_threshold_pct}% (اسکن)")

    problems: List[str] = []
    if geo["trailing_gap"] < geo["cost"]:
        problems.append(
            f"فاصلهٔ تریلینگ ({fmt_pct(geo['trailing_gap'])}) از هزینهٔ رفت‌وبرگشت "
            f"({fmt_pct(geo['cost'])}) کمتر است ⇒ هر خروج تریلینگ خالص منفی است و "
            f"گارد هزینه تمام ورودها را رد می‌کند."
        )
    if geo["required_move"] > float(cfg.min_observed_move_pct):
        problems.append(
            f"حرکت لازم ({fmt_pct(geo['required_move'])}) از آستانهٔ سیگنال "
            f"({fmt_pct(float(cfg.min_observed_move_pct))}) بیشتر است ⇒ سیگنال‌ها "
            f"همه رد می‌شوند."
        )
    if hold <= 0:
        problems.append(
            "توقف زمانی خاموش است (`max_hold_minutes = 0`) ⇒ در مطالعهٔ ۱۰۰ روزه، "
            "پوزیشن‌های بی‌حرکت به‌طور میانگین ۱٫۲ تا ۲٫۱ روز باز ماندند و سرمایه "
            "را قفل کردند. پروفایل آزمون ۳۶۰ دقیقه است (بند ۱۱-۶ تحلیل)."
        )
    if float(cfg.paper_half_spread_pct) <= 0 and str(cfg.execution_mode).lower() != "real":
        problems.append(
            "نیم‌اسپرد صفر است ⇒ معاملات کاغذی با قیمت میانه پر می‌شوند و نتیجه "
            "خوش‌بینانه‌تر از واقعیت گزارش می‌شود."
        )

    if problems:
        print("\n🔴 ایرادهای پیکربندی:")
        for item in problems:
            print(f"   • {item}")
    else:
        print("\n🟢 هندسهٔ هزینه با آستانه‌های ورود سازگار است: "
              "سیگنال‌هایی که رد می‌شوند «بی‌کیفیت»اند، نه «همه».")


def online_report(cfg, pairs: List[str]) -> int:
    print("\n── اتصال زنده به نوبیتکس ──")
    try:
        from trading.nobitex_client import NobitexClient
    except Exception as exc:  # pragma: no cover - import guard
        print(f"🔴 ماژول کلاینت نوبیتکس قابل بارگذاری نیست: {exc}")
        return 2
    try:
        client = NobitexClient(quote_currency=cfg.quote_currency)
        rows = client.get_all_market_stats(cfg.quote_currency)
    except Exception as exc:
        print(f"🔴 دریافت داده از نوبیتکس ناموفق بود: {exc}")
        print("   اگر خطای DNS/شبکه است، این ماشین به api نوبیتکس دسترسی ندارد؛")
        print("   اسکریپت را روی همان سیستمی اجرا کنید که ربات قرار است کار کند.")
        return 2

    wanted = {p.upper() for p in pairs}
    matched = [r for r in rows if str(r.get("Pair", "")).upper() in wanted]
    if not matched:
        print(f"🔴 هیچ‌کدام از جفت‌ارزهای {sorted(wanted)} در پاسخ نوبیتکس نبود.")
        return 2

    st = build_shadow_tracker(cfg)
    print(f"تعداد بازارهای دریافتی: {len(rows)} | بررسی‌شده: {len(matched)}")
    header = (f"{'جفت‌ارز':<10}{'قیمت':>16}{'اسپرد':>9}{'هزینهٔ RT':>11}"
              f"{'حرکت لازم':>11}  وضعیت")
    print(header)
    print("-" * len(header))
    blocked = 0
    for row in matched:
        pair = str(row.get("Pair", "")).upper()
        bid = float(row.get("Bid") or 0.0)
        ask = float(row.get("Ask") or 0.0)
        price = float(row.get("Price") or 0.0)
        spread = ((ask - bid) / bid * 100.0) if (bid > 0 and ask > 0) else 0.0
        cost = st.round_trip_cost_pct(row)
        guard = st.cost_guard_reason(row, float(cfg.min_observed_move_pct))
        status = "✅ ورود مجاز" if not guard else "⛔ " + guard.split("(")[0].strip()
        if guard:
            blocked += 1
        print(f"{pair:<10}{price:>16,.0f}{spread:>8.2f}%{cost:>10.2f}%"
              f"{float(cfg.min_edge_multiple) * cost:>10.2f}%  {status}")

    total_cost = st.round_trip_cost_pct(matched[0])
    print(f"\nهزینهٔ واقعی رفت‌وبرگشت روی {matched[0].get('Pair')}: "
          f"{fmt_pct(total_cost)} ≈ {cfg.fixed_position_quote * total_cost / 100.0:,.0f} تومان "
          f"به‌ازای هر پوزیشن {cfg.fixed_position_quote:,.0f} تومانی")
    if blocked:
        print(f"⛔ {blocked} از {len(matched)} جفت‌ارز همین حالا توسط گارد هزینه رد می‌شوند؛"
              " ورود فقط روی بازارهایی انجام می‌شود که هندسه‌شان اجازه بدهد.")
    else:
        print("🟢 گارد هزینه هیچ‌کدام از جفت‌ارزهای پیکربندی‌شده را رد نمی‌کند.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="پیش‌پرواز تست نوبیتکس")
    # Same resolution the app itself uses (<project>/data/bot_config.json, or
    # $CRYPTOSCANNER_APPDATA when set) - a relative path would be resolved
    # against the app-data directory, not the current working directory.
    parser.add_argument("--config", default=DEFAULT_CONFIG_FILE)
    parser.add_argument("--offline", action="store_true",
                        help="بدون تماس با شبکه، فقط حساب‌وکتاب هزینه")
    parser.add_argument("--pair", action="append", default=None,
                        help="جفت‌ارز برای بررسی (قابل تکرار؛ پیش‌فرض: trading_pairs)")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except Exception as exc:
        print(f"🔴 خواندن پیکربندی ناموفق بود: {exc}")
        return 2

    pairs = [str(p).upper() for p in (args.pair or cfg.trading_pairs or [])]
    if not pairs:
        print("🔴 هیچ جفت‌ارزی در پیکربندی یا آرگومان --pair تعیین نشده است.")
        return 2

    print("═" * 62)
    print(" پیش‌پرواز تست نوبیتکس — NobitexAgentWin")
    print(f" پیکربندی: {args.config}")
    print("═" * 62)
    offline_report(cfg)
    if args.offline:
        return 0
    return online_report(cfg, pairs)


if __name__ == "__main__":
    raise SystemExit(main())
