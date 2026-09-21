# Operations Notes

## Restart procedure after the max-drawdown kill-switch fires

`main.py` liquidates all positions and exits the process (`break` out of
the trading loop) when portfolio drawdown from session-start equity
reaches `MAX_DRAWDOWN_STOP` (config.py, default -10%). See the
`[ALERT] MAX DRAWDOWN` log line / Discord alert.

Nothing in the code currently prevents an orchestrator (Docker restart
policy, systemd, a supervisor script) from immediately relaunching the
bot with the exact same config that just produced a 10% loss. Before
restarting after this fires, manually work through:

1. **Pull the trade history for the session that just ended.** Query
   `realized_pnl` (now includes `entry_confidence`, `entry_regime`,
   `exit_reason`, `slippage_exit_pct` — see KNOWN_ISSUES.md) for the
   losing streak. Was it concentrated in one symbol/regime, or spread
   across all three? Sentinel's per-symbol consecutive-loss counter
   (`sentinel.py`) already isolates single-symbol breakage during
   normal operation — a portfolio-wide 10% drawdown despite that
   suggests something more systemic (a regime the model wasn't built
   for, a data-feed problem, a correlated crypto-wide selloff).
2. **Check for any correctness bugs**, not just bad luck — this
   codebase's git history shows several silent-failure bugs (missing
   `await`, wrong argument, stale data) that directly caused realized
   losses before being caught. Rule those out first.
3. **Decide whether to change anything** (position sizing tiers, brain
   weights, `MAX_TOTAL_EXPOSURE_PCT`, disabling a symbol) before
   resuming — or consciously decide the loss was within expected
   variance and resume unchanged. Either way, this should be a
   deliberate decision, not a default.
4. Only then restart. `start_equity` resets to whatever equity is at
   that moment, so the next 10% drawdown budget is measured from the
   post-loss balance, not the original one.

This is a process checklist, not enforced by code — nothing currently
blocks an automatic restart. If this bot is run under a
restart-on-exit supervisor, consider having the supervisor treat this
specific exit path differently (e.g. a distinct exit code that the
supervisor does NOT auto-restart on) so step 1-3 can't be skipped by
infrastructure automatically bringing the process back up.
