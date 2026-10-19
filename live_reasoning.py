"""Optional, request-fenced presentation for Hermes' external-process nonstream path.

Never changes host dispatch, callbacks, or authentication. Only the nonstream
collector uses this bridge; the normal response retains all reasoning and usage.
Host internals are read-only compatibility seams: absence disables presentation.
"""
from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
import sys
import threading
import uuid
import weakref
from typing import Any

PROVIDER = "antigravity-oauth"
_lease: ContextVar[Any] = ContextVar("antigravity_live_reasoning_request", default=None)
_registration_lock = threading.RLock()
_registrations = weakref.WeakKeyDictionary()


def _home():
    from hermes_constants import get_hermes_home
    return str(get_hermes_home().resolve())


def _live_session(session_id, home) -> Any:
    # Do not import/start a gateway just to display optional output.
    server = sys.modules.get("tui_gateway.server")
    finder = getattr(server, "_find_live_session_by_key", None)
    if not callable(finder):
        return None
    live = finder(session_id, home)
    if live is None:
        # The gateway stores its launch profile as None, not an absolute home.
        launch_home = getattr(server, "_launch_home", None)
        if callable(launch_home) and str(Path(str(launch_home())).resolve()) == home:
            live = finder(session_id, None)
    return live


def _resolve_agent(session_id, home):
    from agent.subagent_lifecycle import get_active_subagent_parent
    agent = get_active_subagent_parent()
    live = _live_session(session_id, home)
    if agent is not None:
        # A mismatching bound context must NOT fall back to another session.
        if getattr(agent, "session_id", None) != session_id:
            return None, None
        if getattr(agent, "platform", None) == "tui" and live is None:
            return None, None
        if live and live[1].get("agent") is not agent:
            return None, None
        return agent, live
    return (live[1].get("agent"), live) if live else (None, None)


class _Lease:
    def __init__(self, agent, live, session_id, turn_id, request_id, token, home, model):
        self.agent = agent
        self.callback = agent.reasoning_callback
        self.live = live
        self.session_id = session_id
        self.turn_id = turn_id
        self.request_id = request_id
        self.token = token
        self.home = home
        self.model = model
        self.active = True
        self.claimed = False
        self.lock = threading.RLock()

    def current(self):
        a = self.agent
        if not self.active or _home() != self.home:
            return False
        if (getattr(a, "provider", None) != PROVIDER
                or getattr(a, "session_id", None) != self.session_id
                or getattr(a, "model", None) != self.model
                or getattr(a, "_current_turn_id", None) != self.turn_id
                or getattr(a, "_current_api_request_id", None) != self.request_id
                or getattr(a, "reasoning_callback", None) is not self.callback
                or not callable(self.callback)):
            return False
        if (getattr(a, "is_subagent", False)
                or getattr(a, "side_agent", False)
                or getattr(a, "platform", "") in {"cron", "subagent", "background"}
                or getattr(a, "_interrupt_requested", False)
                or getattr(a, "_mute_notification_reply", False)
                or getattr(a, "_closed", False)):
            return False
        active = getattr(a, "_model_request_active", None)
        if active is None or not active.is_set():
            return False
        # The host otherwise replays the full reasoning after the response. Only
        # bridge surfaces where its existing stream-consumer gate suppresses that.
        if not (getattr(a, "_stream_callback", None)
                or getattr(a, "stream_delta_callback", None)):
            return False
        if self.live is not None:
            live = _live_session(self.session_id, self.home)
            if not live or live[0] != self.live[0] or live[1] is not self.live[1]:
                return False
            s = live[1]
            if (s.get("agent") is not a or not s.get("running")
                    or s.get("_closing") or s.get("_finalized")):
                return False
        return True

    def emit(self, text, client):
        # Revocation and delivery are serialized: once middleware exits there is
        # no remaining callback in flight, even if a daemon outlives cancellation.
        with self.lock:
            try:
                if (isinstance(text, str) and text and self.current()
                        and callable(self.callback) and not getattr(client, "is_closed", False)):
                    self.callback(text)  # installed show/hide + mute gates remain authoritative
            except Exception:
                self.active = False  # optional presentation must never break completion

    def revoke(self):
        with self.lock:
            self.active = False
            self.agent = self.callback = self.live = None


def execution_middleware(*, request, next_call, provider="", session_id="",
                         turn_id="", api_request_id="", model="", **context):
    lease = None
    token = None
    forwarded = request
    try:
        if (provider == PROVIDER and isinstance(request, dict)
                and request.get("_agy_session_id") == session_id
                and all(isinstance(v, str) and v for v in (session_id, turn_id, api_request_id))
                and not request.get("stream") and not context.get("aux_task")):
            home = _home()
            agent, live = _resolve_agent(session_id, home)
            if agent is not None:
                lease = _Lease(agent, live, session_id, turn_id, api_request_id,
                               uuid.uuid4().hex, home, model)
                if lease.current():
                    forwarded = {**request, "_agy_request_token": lease.token}
                else:
                    lease = None
    except Exception:
        lease = None
    # Mask outer leases for nested/auxiliary executions, including rejected ones.
    token = _lease.set(lease)
    try:
        return next_call(forwarded)
    finally:
        if lease is not None:
            lease.revoke()
        _lease.reset(token)


def collector_emitter(session_id=None, request_token=None, *, client=None):
    """Claim at most one collector for this exact physical request."""
    lease = _lease.get()
    if lease is None:
        return None
    with lease.lock:
        try:
            if (lease.claimed or session_id != lease.session_id
                    or request_token != lease.token or not lease.current()):
                return None
            lease.claimed = True
            return lambda text: lease.emit(text, client)
        except Exception:
            return None


def register(ctx):
    return ctx.register_middleware("llm_execution", execution_middleware)


def ensure_registered():
    """Provider kinds skip general register(ctx); bootstrap via the provider hook.

    Use the public registration facade and its unloadable handle, never mutate a
    host registry or function. Re-register after manager unload on the next main
    request projection. No auxiliary projection calls this function.
    """
    try:
        from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
        manager = get_plugin_manager()
        with _registration_lock:
            handle = _registrations.get(manager)
            if handle is None or not handle.active:
                manifest = PluginManifest(name=PROVIDER, kind="model-provider", source="user",
                                          path=str(Path(__file__).resolve().parent))
                handle = register(PluginContext(manifest, manager))
                _registrations[manager] = handle
    except Exception:
        pass  # older/isolated hosts still receive the full normal completion
