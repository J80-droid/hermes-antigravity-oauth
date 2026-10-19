# Plugin-only nonstream reasoning delivery verification

## Outcome

Real bare `gemini-4-argon`, existing agy account, unchanged `external_process`
authentication and `_should_stream=False` dispatch:

- First real TUI `reasoning.delta` frame: **13.450841 s**.
- Agy final result consumed by stream reader: **28.486059 s**.
- `perform_api_call` returned: **28.486883 s**.
- Three reasoning events; completed reasoning 967 characters; answer 2180 characters.
- Finish `stop`, no tool calls. Usage remained 13910 input / 2258 output /
  16168 total, with 1217 reported reasoning tokens.

Machine-readable receipt: `verification-live-reasoning-final.json`.

This was an isolated **actual AIAgent instance** with its real kwargs builder,
provider discovery, registered execution middleware, `perform_api_call`, normal
nonstream worker, real agy subprocess, existing DB thinking reader, collector,
and installed `server._agent_cbs(...)[reasoning_callback]`. The harness bound
that actual agent through `bind_subagent_parent`, as the conversation facade
does, and assigned current turn/request identifiers. It did not override the
streaming gate, provider auth, client creation, dispatch or collector.

Only external UI egress `server.write_json` was captured in this isolated
process. A Python profiling observer recorded the reader result-consumption
instant and collector worker thread without replacing either function. This
proves pre-completion TUI event delivery, **not visible rendering in the user's
existing desktop/TUI**, and not history restoration. It starts at request
construction/dispatch, not a full `run_conversation` with auxiliary services.

Command: `venv/bin/python3.11 scripts/verify_live_reasoning.py` (Hermes checkout
venv path). Probe calls `hermes_bootstrap` before other host imports. It closes
the agent client, deletes its temporary workspace, removes the isolated TUI
session, and removes its profiling observer. No running gateway was restarted.

## Prior probes / harness issues

Three real provider calls were used, with the same bare model/account:

1. First produced 197 reasoning characters and one captured JSON-RPC frame;
   the harness wrongly searched the frame root rather than `params.type`, so
   its assertion failed. This was a harness failure, not accepted live evidence.
2. Corrected harness, same easy task: successful answer but zero thinking text
   and zero reported reasoning tokens. It did not establish live delivery.
   Original output retained in `verification-live-reasoning.stderr`; the paired
   stdout `.json` is empty because importing the TUI server redirects print.
3. More reasoning-dependent tool-free probability task: successful evidence above.

Agy sometimes supplies no reasoning; the adapter never fabricates it.

## Regression coverage

**248 tests passed, no skips** in 7.427 s on final sources; full output in
`verification-tests-final.log`. The preceding bootstrapped run (7.387 s) is
retained in `verification-tests-bootstrapped.log`.

```
<hermes-checkout>/venv/bin/python3.11 -c "import sys; sys.path.insert(0, '<hermes-checkout>'); import hermes_bootstrap; import unittest; suite=unittest.defaultTestLoader.discover('tests', pattern='test_*.py'); result=unittest.TextTestRunner(verbosity=2).run(suite); raise SystemExit(not result.wasSuccessful())"
```

Earlier plain discovery: 248 tests, 3 auxiliary tests skipped because installed
Hermes bootstrap had not activated dependencies before those imports; preserved
in `verification-tests.log`. The bootstrapped run above resolved the test setup
issue. Other existing warning output includes unrelated nerve plugin load error
and temporary-directory resource warnings; no test failed.

New coverage (19 tests across `test_live_reasoning_adapter.py` and
`test_live_reasoning_host.py`) includes:

- Real host gate, middleware and nonstream worker/collector path (synthetic agy
  producer in regressions; separately real producer in the live probe).
- Exact session/profile/turn/request/model/provider and callback identity fences.
- Context propagation into workers; concurrent equal session IDs in distinct profiles.
- No identity, no host, missing TUI session, wrong context, profile-aware fallback.
- Launch-profile `None` sentinel handling without unscoped session lookup.
- Revocation, cancellation, retry, late copied contexts, closing/replaced sessions.
- Actual TUI hidden/muted gates; callback exceptions remain optional.
- Streaming callers emit no duplicate bridge callbacks; post-response host
  reasoning extraction emits no duplicate either.
- Worker and oneshot collectors; full reasoning, answer, tools and usage preserved.
- Actual provider projection installs middleware once and restores a disposed
  registration; auxiliary projection carries no identity.

Existing prompt tests continue to verify reasoning is not replayed to agy.

## Compatibility / activation boundary

Provider-kind plugins do not receive ordinary `register(ctx)` loading in this
Hermes version. The provider's main-request projection therefore bootstraps its
middleware with the existing `PluginContext` registration API. No host function
or registry is monkeypatched by production plugin code. Only scalar session and
random per-request identity travel in API kwargs. Leases are context-local,
revoked in `finally`, and release retained agent/callback references on exit.

Read-only host seams (`get_active_subagent_parent`, current request attributes,
loaded TUI session finder) may change in future Hermes releases; optional live
presentation fails closed and ordinary completed responses remain intact.

Already-running processes have not reloaded these files. No activation in an
existing window is claimed. No routes, account, model configuration, auth type,
core source, or service lifecycle were changed. No commit was created.

Core HEAD observed: `e06df9c2eae6901f4c88f464e0ae6e753c0428b8`.
Core `git status --short` remained empty; plugin `git diff --check` passed.
Pre-existing edits in prompt/stream/client and the DB-thinking tests were retained.

## Reusable verification lessons (plugin-local scope)

- Model-provider discovery skips general lifecycle registration: exercise the
  actual request projection rather than assuming `register(ctx)` ran.
- TUI wire event names live at `params.type`, not the frame root.
- Launch-profile live session records use `profile_home=None`; normalize only
  after positively matching `_launch_home`, never perform a bare-ID fallback.
- Import installed `hermes_bootstrap` before host dependencies in standalone
  harnesses: it can re-exec into the current managed Python environment.
- Require a timestamped real callback event before final result consumption;
  preserved final reasoning and synthetic streams alone cannot prove visibility.
