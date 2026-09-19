from trading.execution_mode import (
    LIVE,
    PAPER,
    cycle_plan,
    normalize_execution_mode,
    should_run_live_tracker,
)


def test_normalize_defaults_to_paper():
    assert normalize_execution_mode(None) == PAPER
    assert normalize_execution_mode("") == PAPER
    assert normalize_execution_mode("unknown") == PAPER
    assert normalize_execution_mode("paper") == PAPER
    assert normalize_execution_mode("SHADOW") == PAPER
    assert normalize_execution_mode("live") == LIVE
    assert normalize_execution_mode("REAL") == LIVE
    assert normalize_execution_mode("nobitex") == LIVE


def test_cycle_plan_paper_never_opens_live():
    plan = cycle_plan(PAPER, live_entries_enabled=True)
    assert plan["mode"] == PAPER
    assert plan["open_paper"] is True
    assert plan["open_live"] is False
    assert plan["evaluate_signals"] is True
    assert plan["monitor_live"] is True
    assert plan["scan_tag"] == "[PAPER]"


def test_cycle_plan_live_entries_require_start():
    paused = cycle_plan(LIVE, live_entries_enabled=False)
    assert paused["open_paper"] is False
    assert paused["open_live"] is False
    assert paused["evaluate_signals"] is False
    assert paused["monitor_live"] is True
    assert paused["scan_tag"] == "[REAL]"

    armed = cycle_plan(LIVE, live_entries_enabled=True)
    assert armed["open_paper"] is False
    assert armed["open_live"] is True
    assert armed["evaluate_signals"] is True


def test_paper_and_live_entries_are_mutually_exclusive():
    paper = cycle_plan("paper", True)
    live = cycle_plan("live", True)
    assert paper["open_paper"] != live["open_paper"]
    assert paper["open_live"] != live["open_live"]
    assert not (paper["open_paper"] and paper["open_live"])
    assert not (live["open_paper"] and live["open_live"])


def test_paper_scan_skips_live_tracker_without_leftover_positions():
    plan = cycle_plan(PAPER, live_entries_enabled=True)
    assert plan["monitor_live"] is True
    assert should_run_live_tracker(plan, False) is False
    assert should_run_live_tracker(plan, True) is True


def test_live_start_always_runs_live_tracker():
    plan = cycle_plan(LIVE, live_entries_enabled=True)
    assert should_run_live_tracker(plan, False) is True
    assert should_run_live_tracker(plan, True) is True


def test_live_paused_only_monitors_leftover_positions():
    plan = cycle_plan(LIVE, live_entries_enabled=False)
    assert plan["open_live"] is False
    assert should_run_live_tracker(plan, False) is False
    assert should_run_live_tracker(plan, True) is True
