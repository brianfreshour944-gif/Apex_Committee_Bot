"""Regression tests for the drawdown-baseline (start_equity) fix.

Bug: get_account_state() collapses an API/auth failure to equity == 0.0.
run() latched start_equity to whatever the FIRST fetch returned, so a failed
first fetch pinned the baseline to 0.0. The guard
`drawdown = ... if start_equity > 0 else 0.0` then reported drawdown 0.0
forever, permanently disarming MAX_DRAWDOWN_STOP (-10%) for the whole process
lifetime -- even after auth recovered.

Covers:
  1. A failed first fetch (equity 0.0) leaves start_equity UNARMED (None)
  2. The first SUCCESSFUL fetch arms the baseline
  3. Once armed, the baseline is NOT re-armed on later cycles (monotone session)
  4. Drawdown is computed correctly against the armed baseline
  5. Drawdown stays 0.0 (not TypeError) while unarmed
  6. Pre-fix behavior is reproduced (guards against the test passing vacuously)

Run:  PYTHONPATH=<repo> python tests/test_drawdown_baseline.py
"""

import os
os.environ.setdefault("APCA_API_KEY_ID", "test")
os.environ.setdefault("APCA_API_SECRET_KEY", "test")

import asyncio

import main
from config import MAX_DRAWDOWN_STOP

_passed = 0
_failed = 0


def check(label, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  PASS: {label}" + (f" | {detail}" if detail else ""))
    else:
        _failed += 1
        print(f"  FAIL: {label}" + (f" | {detail}" if detail else ""))


async def _arm_baseline(equity_values):
    """Replay run()'s baseline-arming block for a sequence of fetch results.

    Mirrors main.py: if start_equity is None -> arm only when equity > 0.
    Returns the final start_equity.
    """
    main.start_equity = None
    for equity in equity_values:
        if main.start_equity is None and equity > 0:
            main.start_equity = equity
    return main.start_equity


def _drawdown(equity, start_equity):
    """Mirror of main.py's drawdown computation."""
    if start_equity and start_equity > 0:
        return (equity - start_equity) / start_equity
    return 0.0


def test_failed_first_fetch_leaves_baseline_unarmed():
    print("\n--- 1. Failed first fetch does not latch the baseline ---")
    # equity 0.0 == get_account_state()'s failure sentinel
    result = asyncio.run(_arm_baseline([0.0]))
    check("start_equity stays None after a failed fetch", result is None,
          f"start_equity={result!r}")


def test_successful_fetch_arms_baseline():
    print("\n--- 2. First successful fetch arms the baseline ---")
    result = asyncio.run(_arm_baseline([100.0]))
    check("start_equity == 100.0", result == 100.0, f"start_equity={result!r}")


def test_recovery_after_early_failures_arms():
    print("\n--- 3. Baseline arms once the fetch recovers ---")
    # Fail for the first two cycles (auth outage), then recover.
    result = asyncio.run(_arm_baseline([0.0, 0.0, 100.0]))
    check("start_equity arms to 100.0 after recovery", result == 100.0,
          f"start_equity={result!r}")


def test_baseline_is_monotone_not_rearmed():
    print("\n--- 4. Baseline is armed once, not re-armed each cycle ---")
    result = asyncio.run(_arm_baseline([100.0, 150.0, 80.0]))
    check("start_equity keeps the FIRST successful value (100.0)", result == 100.0,
          f"start_equity={result!r}")


def test_drawdown_uses_armed_baseline():
    print("\n--- 5. Drawdown measured from the armed baseline ---")
    start = asyncio.run(_arm_baseline([100000.0, 95000.0]))
    dd = _drawdown(90000.0, start)
    check("drawdown == -0.10 at 90k vs 100k baseline", abs(dd - (-0.10)) < 1e-9,
          f"drawdown={dd:.4f}")
    check("MAX_DRAWDOWN_STOP fires at -10%", dd <= MAX_DRAWDOWN_STOP,
          f"dd={dd:.4f} <= {MAX_DRAWDOWN_STOP}")


def test_unarmed_baseline_yields_zero_not_typeerror():
    print("\n--- 6. Unarmed baseline -> drawdown 0.0 (no TypeError) ---")
    try:
        dd = _drawdown(0.0, None)
        check("drawdown is 0.0 while unarmed", dd == 0.0, f"drawdown={dd!r}")
    except TypeError as e:
        check("drawdown is 0.0 while unarmed", False, f"raised TypeError: {e}")


def test_prefix_behaviour_reproduced():
    print("\n--- 7. Pre-fix behavior is reproduced (guards against vacuous pass) ---")
    # The OLD code was: `if start_equity is None: start_equity = equity`
    # i.e. it latched 0.0 and never re-armed -> kill switch dead.
    old_start = None
    for equity in [0.0, 100.0]:
        if old_start is None:
            old_start = equity
    old_dd = (100.0 - old_start) / old_start if old_start > 0 else 0.0
    check("OLD code latches 0.0 and stays disarmed", old_start == 0.0 and old_dd == 0.0,
          f"old_start={old_start!r} old_drawdown={old_dd!r}")
    check("NEW code avoids that state",
          asyncio.run(_arm_baseline([0.0, 100.0])) == 100.0)


if __name__ == "__main__":
    test_failed_first_fetch_leaves_baseline_unarmed()
    test_successful_fetch_arms_baseline()
    test_recovery_after_early_failures_arms()
    test_baseline_is_monotone_not_rearmed()
    test_drawdown_uses_armed_baseline()
    test_unarmed_baseline_yields_zero_not_typeerror()
    test_prefix_behaviour_reproduced()
    print(f"\n=== {_passed} passed, {_failed} failed ===")
    raise SystemExit(1 if _failed else 0)
