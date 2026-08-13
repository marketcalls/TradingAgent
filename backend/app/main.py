"""FastAPI app: SSE chat over an Agno agent, plus the order confirmation loop.

Streaming facts verified against agno 2.8.7:
  - agent.arun(stream=True) returns an async generator DIRECTLY. Do not await it.
  - stream_events=True is required or no tool events are emitted at all.
  - RunPausedEvent is yielded UNCONDITIONALLY in streaming mode, even with
    stream_events=False, and the stream TERMINATES there with no trailing
    RunCompleted. A generator that waits for a terminal event after a pause hangs the
    browser forever, so the pause closes the response.
  - continue_run's stream_events defaults to False, not None. It must be passed
    explicitly or the resume leg emits no tool events and the UI timeline goes blank
    mid-trade.
  - Errors no longer raise; they arrive as RunErrorEvent and the stream ends.
  - ToolCallCompleted fires with tool_call_error=True and is then followed by a
    separate ToolCallError, so the second is suppressed to avoid double counting.
  - A cancelled run still emits a trailing RunCompleted, so RunCancelled is terminal.
"""

from __future__ import annotations

import copy
import json
import re
import logging
import threading
from typing import Any, AsyncIterator

from .config import get_settings, setup_logging

setup_logging()

from agno.db.base import SessionType  # noqa: E402
from agno.run.requirement import RunRequirement  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from .agent import build_agent  # noqa: E402
from .openalgo.client import get_client  # noqa: E402
from .openalgo.normalize import sanitize_text  # noqa: E402
from .safety.audit import get_audit_log  # noqa: E402
from .version import get_version  # noqa: E402

log = logging.getLogger("app")

settings = get_settings()
agent = build_agent(settings)
client = get_client()

# One Agent per reasoning-effort level, built on demand. They all share the same
# SqliteDb file, so a session (and a paused run) is readable from any of them and the
# user can change the setting mid-conversation without losing history.
_agents: dict[str, Any] = {"": agent}
_agents_lock = threading.Lock()


def get_agent(effort: str | None = None):
    """Return the agent for a reasoning effort. Empty or unknown means the .env default."""
    key = (effort or "").strip().lower()
    if key and key not in settings.REASONING_EFFORTS:
        log.warning("ignoring unknown reasoning_effort %r from the client", key)
        key = ""
    if not key:
        return agent
    with _agents_lock:
        built = _agents.get(key)
        if built is None:
            scoped = copy.copy(settings)
            scoped.litellm_reasoning_effort = key
            built = build_agent(scoped)
            _agents[key] = built
            log.info("built agent for reasoning_effort=%s", key)
        return built

app = FastAPI(title="OpenAlgo Trading Agent", version=get_version())
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

USER_ID = "local"

# Tools whose confirmation card should carry a priced preview.
_ORDER_TOOLS = {
    "place_order", "place_smart_order", "place_basket_order", "place_split_order",
    "modify_order", "place_gtt_order", "place_options_order",
    "place_options_multi_order",
}


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    trading_enabled: bool | None = None
    # "" or absent uses the .env default; otherwise none|minimal|low|medium|high.
    reasoning_effort: str | None = None


class ConfirmRequest(BaseModel):
    run_id: str
    session_id: str
    decisions: dict[str, bool]
    notes: dict[str, str] | None = None
    reasoning_effort: str | None = None


# A model can claim to have placed an order without calling the tool. Measured on
# gpt-5.6-luna: 5 of 6 order instructions reached the confirmation gate, and the sixth
# answered "Placing the following order in analyzer mode" having called nothing.
#
# This is not a safety hole - nothing can reach the broker except through a gated tool -
# but it is a HONESTY hole, and the more dangerous direction of the two to leave alone,
# because the user walks away believing they have a position they do not have. The
# stream therefore carries an explicit correction when the claim and the tool calls
# disagree.
_ORDER_CLAIM_RE = re.compile(
    r"\b(plac(?:ing|ed)|submitt(?:ing|ed)|execut(?:ing|ed)|sent)\b[^.]{0,60}\border\b"
    r"|\border\b[^.]{0,40}\b(placed|submitted|executed|filled|sent)\b",
    re.IGNORECASE,
)
_MUTATING_TOOLS = _ORDER_TOOLS | {
    "cancel_order", "cancel_all_orders", "close_all_positions", "manage_gtt_order",
}


def _unbacked_order_claim(text: str, tools_called: set[str]) -> bool:
    """True when the reply claims an order happened but no order tool ran."""
    if tools_called & _MUTATING_TOOLS:
        return False
    return bool(_ORDER_CLAIM_RE.search(text or ""))


def sse(event: str, data: dict[str, Any]) -> str:
    return f"data: {json.dumps({'type': event, **data}, default=str, ensure_ascii=False)}\n\n"


def _title_of(session_data: Any) -> str:
    """session_data is double-encoded JSON in Agno's SQLite schema."""
    v = session_data
    for _ in range(2):
        if isinstance(v, (bytes, bytearray)):
            v = v.decode()
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except (ValueError, TypeError):
                return "New chat"
    return (v or {}).get("session_name") or "New chat" if isinstance(v, dict) else "New chat"


def _name_session(session_id: str, first_message: str) -> None:
    title = " ".join(first_message.split())[:60].strip()
    if len(first_message) > 60:
        title += "..."
    try:
        agent.db.rename_session(session_id=session_id, session_type=SessionType.AGENT,
                                session_name=title or "New chat")
    except Exception:  # noqa: BLE001
        log.warning("could not name session %s", session_id)


def _preview_for(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Server-computed context for the confirmation card.

    The model does not produce these numbers; the backend does. That is the point -
    the card must show something the model cannot get wrong.
    """
    # `mode` is the canonical key the confirmation card reads to decide whether to paint
    # itself as a live order. It must be the mode this order is priced under, not the
    # UI's polled banner value, which can be stale. analyzer_mode is kept as an alias.
    current_mode = client.analyzer_mode()
    preview: dict[str, Any] = {"mode": current_mode, "analyzer_mode": current_mode}
    if tool_name not in _ORDER_TOOLS:
        return preview

    try:
        symbol = str(args.get("symbol") or "")
        exchange = str(args.get("exchange") or "")
        quantity = args.get("quantity")

        if symbol and exchange:
            ltp = client.ltp(symbol, exchange)
            preview["ltp"] = ltp
            if ltp and quantity:
                try:
                    preview["notional"] = round(float(quantity) * float(ltp), 2)
                except (TypeError, ValueError):
                    pass

            positions = client.call_enveloped("positionbook")
            if positions.get("ok") and isinstance(positions.get("data"), list):
                for row in positions["data"]:
                    if str(row.get("symbol", "")).upper() == symbol.upper():
                        preview["current_position"] = row.get("quantity")
                        preview["position_pnl"] = row.get("pnl")
                        break
                preview.setdefault("current_position", 0)

        legs = args.get("orders") or args.get("legs")
        if isinstance(legs, list):
            preview["leg_count"] = len(legs)

        funds = client.call_enveloped("funds")
        if funds.get("ok") and isinstance(funds.get("data"), dict):
            cash = funds["data"].get("availablecash")
            # available_funds is the key the confirmation card promotes; the alias keeps
            # the OpenAlgo-native spelling available too.
            preview["available_funds"] = cash
            preview["available_cash"] = cash
    except Exception:  # noqa: BLE001
        log.warning("preview failed for %s", tool_name, exc_info=True)
    return preview


def _requirement_payload(req: Any) -> dict[str, Any]:
    data = req.to_dict()
    te = getattr(req, "tool_execution", None)
    tool_name = getattr(te, "tool_name", "") or ""
    tool_args = getattr(te, "tool_args", {}) or {}
    data["preview"] = _preview_for(tool_name, tool_args)
    return data


async def _pump(stream, request: Request, on_complete=None) -> AsyncIterator[str]:
    """Translate agno run events into the SSE contract."""
    run_id: str | None = None
    terminal = False
    paused = False
    errored_tools: set[str] = set()
    tools_called: set[str] = set()
    said: list[str] = []

    try:
        async for ev in stream:
            name = getattr(ev, "event", None)
            if run_id is None:
                run_id = getattr(ev, "run_id", None)

            # Do not cancel a paused run: it is waiting for the user, not stuck.
            if not paused and await request.is_disconnected():
                if run_id:
                    agent.cancel_run(run_id)
                return

            if name == "RunStarted":
                yield sse("start", {"run_id": ev.run_id, "session_id": ev.session_id})

            elif name == "RunContinued":
                yield sse("start", {"run_id": getattr(ev, "run_id", run_id),
                                    "session_id": getattr(ev, "session_id", None),
                                    "resumed": True})

            elif name == "RunContent":
                text = getattr(ev, "content", None)
                if isinstance(text, str) and text:
                    # Normalised per delta rather than trusted to the prompt. Each delta
                    # is already a decoded str, so no codepoint can be split across two.
                    clean = sanitize_text(text)
                    said.append(clean)
                    yield sse("token", {"delta": clean})

            elif name == "ToolCallStarted":
                t = ev.tool
                tools_called.add(t.tool_name)
                yield sse("tool_start", {"id": t.tool_call_id, "name": t.tool_name,
                                         "args": t.tool_args})

            elif name == "ToolCallCompleted":
                t = ev.tool
                failed = bool(t.tool_call_error)
                if failed:
                    errored_tools.add(t.tool_call_id)
                result = t.result
                if isinstance(result, str) and len(result) > 4000:
                    result = result[:4000] + "..."
                yield sse("tool_end", {
                    "id": t.tool_call_id, "name": t.tool_name, "ok": not failed,
                    "result": result,
                    "duration": getattr(getattr(t, "metrics", None), "duration", None),
                })

            elif name == "ToolCallError":
                t = getattr(ev, "tool", None)
                tid = getattr(t, "tool_call_id", None)
                if tid and tid in errored_tools:
                    continue  # already reported on ToolCallCompleted
                yield sse("tool_end", {
                    "id": tid, "name": getattr(t, "tool_name", None), "ok": False,
                    "result": str(getattr(ev, "error", "tool failed")),
                })

            elif name == "RunPaused":
                terminal = True
                paused = True
                reqs = list(getattr(ev, "active_requirements", []) or [])
                yield sse("confirm", {
                    "run_id": getattr(ev, "run_id", run_id),
                    "session_id": getattr(ev, "session_id", None),
                    "requirements": [_requirement_payload(r) for r in reqs],
                })
                # Do NOT return here. Returning early makes Starlette close this
                # generator, which throws GeneratorExit into agno's generator; its
                # cleanup then marks the run CANCELLED and overwrites the paused state
                # in the database, so the confirm endpoint later finds zero pending
                # requirements. agno yields nothing after RunPaused, so continuing just
                # lets the loop end naturally and the run stay paused.
                continue

            elif name == "RunCancelled":
                terminal = True
                yield sse("done", {"reason": "cancelled"})
                return

            elif name == "RunError":
                terminal = True
                yield sse("error", {"message": str(getattr(ev, "content", "run failed")),
                                    "kind": getattr(ev, "error_type", None)})
                return

            elif name == "RunCompleted":
                terminal = True
                sid = getattr(ev, "session_id", None)
                if on_complete and sid:
                    on_complete(sid)
                if _unbacked_order_claim("".join(said), tools_called):
                    log.warning("model claimed an order without calling an order tool")
                    yield sse("notice", {
                        "level": "warning",
                        "message": "No order was placed. The reply above describes an "
                                   "order, but no order tool was called, so nothing was "
                                   "sent to the broker and you were never asked to "
                                   "approve anything. Ask again to place it.",
                    })
                yield sse("done", {"reason": "stop", "session_id": sid,
                                   "run_id": getattr(ev, "run_id", None)})

    except Exception as exc:  # noqa: BLE001
        log.exception("stream failed")
        yield sse("error", {"message": str(exc), "kind": type(exc).__name__})
        return

    if not terminal:
        yield sse("done", {"reason": "incomplete"})
    # A paused run deliberately emits no `done`: the confirm event was terminal for this
    # request and the run is waiting for the user, not finished.


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@app.get("/api/health")
async def health() -> dict[str, Any]:
    ping = client.ping()
    return {
        "ok": True,
        "version": get_version(),
        "model": settings.litellm_model,
        "missing_keys": settings.missing(),
        "openalgo_connected": bool(ping.get("ok")),
        "broker": (ping.get("data") or {}).get("broker") if ping.get("ok") else None,
        "trading_enabled": settings.trading_enabled,
    }


@app.get("/api/mode")
async def mode() -> dict[str, Any]:
    m = client.analyzer_mode()
    return {
        "mode": m,
        "orders_are_real": m == "live",
        "trading_enabled": settings.trading_enabled,
        "require_analyzer_mode": settings.require_analyzer_mode,
        "kill_switch": settings.kill_switch_engaged(),
        "model": settings.litellm_model,
        "tool_profile": settings.effective_tool_profile,
        # Detected per model, so the UI can hide the control entirely for something
        # like gpt-4o that cannot reason at all.
        "reasoning": settings.reasoning_capability(),
        "default_reasoning_effort": settings.resolve_reasoning_effort() or "",
    }


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest, request: Request) -> StreamingResponse:
    session_id = req.session_id or None
    is_new = session_id is None

    session_state = {
        "trading_enabled": (settings.trading_enabled
                            if req.trading_enabled is None else bool(req.trading_enabled)),
    }

    def on_complete(sid: str) -> None:
        if is_new:
            _name_session(sid, req.message)

    stream = get_agent(req.reasoning_effort).arun(
        req.message,
        session_id=session_id,
        user_id=USER_ID,
        session_state=session_state,
        stream=True,
        stream_events=True,
    )
    return StreamingResponse(_pump(stream, request, on_complete),
                             media_type="text/event-stream", headers=SSE_HEADERS)


@app.post("/api/chat/confirm")
async def chat_confirm(req: ConfirmRequest, request: Request) -> StreamingResponse:
    """Resolve a paused run's requirements and resume it."""
    try:
        run = agent.get_run_output(run_id=req.run_id, session_id=req.session_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, f"run not found: {exc}") from exc
    if run is None:
        raise HTTPException(404, "run not found")

    notes = req.notes or {}
    audit = get_audit_log()
    resolved = 0
    log.info("confirm: run=%s status=%s reqs=%s active=%s decisions=%s",
             req.run_id, getattr(run, "status", None),
             len(getattr(run, "requirements", []) or []),
             len(getattr(run, "active_requirements", []) or []),
             req.decisions)
    for requirement in list(getattr(run, "active_requirements", []) or []):
        if not requirement.needs_confirmation:
            continue
        approved = bool(req.decisions.get(requirement.id))
        te = getattr(requirement, "tool_execution", None)
        if approved:
            requirement.confirm()
        else:
            requirement.reject(notes.get(requirement.id)
                               or "Rejected by the user in the chat interface.")
        resolved += 1
        audit.record(
            "decision", getattr(te, "tool_name", "unknown"),
            dict(getattr(te, "tool_args", {}) or {}),
            session_id=req.session_id, run_id=req.run_id,
            analyzer_mode=client.analyzer_mode(),
            risk_verdict="approved" if approved else "rejected",
            risk_reason=notes.get(requirement.id),
        )

    if not resolved:
        raise HTTPException(400, "this run has no pending confirmations")

    # Same effort as the paused leg, so the resume behaves like the run it continues.
    # Every agent shares one SqliteDb, so the paused run is readable from any of them.
    stream = get_agent(req.reasoning_effort).acontinue_run(
        run_id=req.run_id,
        session_id=req.session_id,
        requirements=run.requirements,
        stream=True,
        # Defaults to False here, unlike arun. Without it the resume leg emits no
        # tool events and the UI timeline goes blank mid-trade.
        stream_events=True,
    )
    return StreamingResponse(_pump(stream, request),
                             media_type="text/event-stream", headers=SSE_HEADERS)


@app.post("/api/chat/{run_id}/cancel")
async def cancel(run_id: str) -> dict[str, Any]:
    return {"cancelled": bool(agent.cancel_run(run_id))}


@app.get("/api/sessions")
async def list_sessions() -> dict[str, Any]:
    rows, total = agent.db.get_sessions(
        session_type=SessionType.AGENT, user_id=USER_ID,
        limit=50, page=1, sort_by="updated_at", sort_order="desc", deserialize=False,
    )
    items = []
    for r in rows:
        r = dict(r)
        items.append({"session_id": r.get("session_id"),
                      "title": _title_of(r.get("session_data")),
                      "updated_at": r.get("updated_at")})
    return {"items": items, "total": total}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    try:
        msgs = agent.get_session_messages(session_id=session_id, skip_roles=["system"])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, f"session not found: {exc}") from exc
    out = []
    for m in msgs or []:
        role = getattr(m, "role", None)
        content = getattr(m, "content", None)
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content})
    return {"session_id": session_id, "messages": out}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, Any]:
    agent.db.delete_session(session_id)
    return {"deleted": True}


@app.get("/api/audit")
async def audit_trail(limit: int = 50, session_id: str | None = None) -> dict[str, Any]:
    return {"items": get_audit_log().recent(limit=limit, session_id=session_id)}
