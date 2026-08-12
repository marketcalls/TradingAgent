"""End-to-end test of the order confirmation loop over HTTP.

This is the test that matters most in the whole project. It drives the real FastAPI app
with a real model and a real broker connection, and proves that:

  1. Proposing an order PAUSES the run and emits `confirm` instead of placing anything.
  2. The paused stream terminates - no trailing `done` - so a browser does not hang.
  3. Rejecting resumes the run without ever reaching the broker.
  4. Approving resumes the run, executes exactly once, and returns an order id.
  5. The resume happens in a DIFFERENT HTTP request, which is the real deployment shape.

Safety: it refuses to run unless OpenAlgo reports analyzer mode, so every order placed
here is simulated. It starts its own uvicorn with TRADING_ENABLED=true rather than
touching the operator's .env.

Run:  python backend/tests/test_confirm_loop.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str]] = []
PORT = 8099
BASE = f"http://127.0.0.1:{PORT}"


def check(name: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    results.append((name, status))
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    results.append((name, SKIP))
    print(f"  [{SKIP}] {name}" + (f" - {detail}" if detail else ""))


def read_sse(response) -> list[dict]:
    events = []
    for line in response.iter_lines():
        if not line:
            continue
        text = line if isinstance(line, str) else line.decode("utf-8")
        if text.startswith("data: "):
            events.append(json.loads(text[6:]))
    return events


def summarize(events: list[dict]) -> str:
    parts = []
    for e in events:
        t = e["type"]
        if t == "tool_start":
            parts.append(f"start:{e['name']}")
        elif t == "tool_end":
            parts.append(f"end:{e['name']}({'ok' if e['ok'] else 'fail'})")
        elif t in ("confirm", "done", "error", "start"):
            parts.append(t if t != "done" else f"done:{e.get('reason')}")
    return " ".join(parts)


def start_server() -> subprocess.Popen:
    env = dict(os.environ)
    env["TRADING_ENABLED"] = "true"        # test-only override, .env is untouched
    env["REQUIRE_ANALYZER_MODE"] = "true"  # keep the live-mode block on
    env["MAX_ORDERS_PER_SESSION"] = "50"
    env["DUPLICATE_ORDER_WINDOW_SEC"] = "0"
    env["LOG_LEVEL"] = "WARNING"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=str(ROOT / "backend"), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        time.sleep(1)
        try:
            if httpx.get(f"{BASE}/api/health", timeout=2).status_code == 200:
                return proc
        except Exception:  # noqa: BLE001
            continue
    proc.terminate()
    raise RuntimeError("server did not start")


def main() -> int:
    print("Confirmation loop end-to-end test")
    settings = get_settings()
    if not settings.openalgo_api_key or not settings.baseten_api_key:
        skip("all checks", "missing OPENALGO_API_KEY or BASETEN_API_KEY")
        return 0

    proc = None
    try:
        print("  starting server on", PORT)
        proc = start_server()

        with httpx.Client(timeout=300.0) as http:
            mode = http.get(f"{BASE}/api/mode").json()
            if mode.get("mode") != "analyze":
                skip("order confirmation checks",
                     f"OpenAlgo is in {mode.get('mode')} mode; refusing to place real orders")
                return 0
            check("server reports analyze mode and trading enabled",
                  mode["mode"] == "analyze" and mode["trading_enabled"] is True,
                  f"mode={mode['mode']} trading={mode['trading_enabled']}")

            # ---------- 1. propose an order: must PAUSE ----------
            print("\n=== propose an order ===")
            with http.stream("POST", f"{BASE}/api/chat/stream", json={
                # CNC, not MIS: the sandbox rejects MIS orders after 15:15 IST square-off, so a
                # MIS-based test is only green in the morning.
                "message": "Buy 1 share of SBIN on NSE at market price, CNC product.",
                "trading_enabled": True,
            }) as r:
                events = read_sse(r)
            print("  events:", summarize(events))

            confirms = [e for e in events if e["type"] == "confirm"]
            check("proposing an order emits confirm", len(confirms) == 1,
                  f"{len(confirms)} confirm events")
            if not confirms:
                for e in events:
                    if e["type"] == "error":
                        print("  ERROR:", e["message"][:300])
                    if e["type"] == "token":
                        continue
                return 1

            conf = confirms[0]
            check("confirm is the terminal event",
                  events[-1]["type"] == "confirm", events[-1]["type"])
            check("no done event follows the pause",
                  not any(e["type"] == "done" for e in events))

            placed = [e for e in events if e["type"] == "tool_end"
                      and e["name"] == "place_order"]
            check("place_order did NOT execute before approval", not placed,
                  f"{len(placed)} executions")

            reqs = conf["requirements"]
            check("one requirement returned", len(reqs) == 1, str(len(reqs)))
            req = reqs[0]
            te = req.get("tool_execution") or {}
            check("requirement names place_order", te.get("tool_name") == "place_order",
                  str(te.get("tool_name")))
            args = te.get("tool_args") or {}
            check("requirement carries the order arguments",
                  str(args.get("symbol", "")).upper() == "SBIN",
                  json.dumps(args)[:110])

            preview = req.get("preview") or {}
            check("preview computed server-side carries ltp",
                  isinstance(preview.get("ltp"), (int, float)),
                  f"ltp={preview.get('ltp')} notional={preview.get('notional')} "
                  f"cash={preview.get('available_cash')} mode={preview.get('analyzer_mode')}")
            # `mode` is the key the confirmation card reads to decide whether to paint
            # itself red. If the backend stops sending it the card silently falls back
            # to the polled banner, which can be stale - so assert it by name.
            check("preview reports the mode under the key the card reads",
                  preview.get("mode") == "analyze" and preview.get("analyzer_mode") == "analyze",
                  f"mode={preview.get('mode')}")
            check("preview exposes available_funds for the card",
                  preview.get("available_funds") is not None,
                  str(preview.get("available_funds")))

            run_id, session_id, req_id = conf["run_id"], conf["session_id"], req["id"]

            # ---------- 2. REJECT, in a separate request ----------
            print("\n=== reject the order ===")
            with http.stream("POST", f"{BASE}/api/chat/confirm", json={
                "run_id": run_id, "session_id": session_id,
                "decisions": {req_id: False},
                "notes": {req_id: "Rejected for testing: position size is too large."},
            }) as r:
                rej_events = read_sse(r)
            print("  events:", summarize(rej_events))

            executed = [e for e in rej_events if e["type"] == "tool_end"
                        and e["name"] == "place_order" and e["ok"]]
            check("a rejected order never reaches the broker", not executed,
                  f"{len(executed)} successful executions")
            check("the rejected run completes",
                  any(e["type"] == "done" for e in rej_events),
                  summarize(rej_events)[-40:])
            reply = "".join(e["delta"] for e in rej_events if e["type"] == "token")
            check("the model acknowledges the rejection", len(reply) > 20,
                  reply.strip()[:90].replace("\n", " "))

            # ---------- 3. propose again and APPROVE ----------
            print("\n=== propose again, then approve ===")
            with http.stream("POST", f"{BASE}/api/chat/stream", json={
                "message": "Buy 1 share of YESBANK on NSE at market, CNC product.",
                "trading_enabled": True,
            }) as r:
                events2 = read_sse(r)
            print("  events:", summarize(events2))
            confirms2 = [e for e in events2 if e["type"] == "confirm"]
            if not confirms2:
                check("second proposal paused", False, summarize(events2))
                return 1
            check("second proposal paused", True)

            conf2 = confirms2[0]
            req2 = conf2["requirements"][0]
            with http.stream("POST", f"{BASE}/api/chat/confirm", json={
                "run_id": conf2["run_id"], "session_id": conf2["session_id"],
                "decisions": {req2["id"]: True},
            }) as r:
                app_events = read_sse(r)
            print("  events:", summarize(app_events))

            ran = [e for e in app_events if e["type"] == "tool_end"
                   and e["name"] in ("place_order", "place_smart_order")]
            check("approving executes the order tool", len(ran) >= 1,
                  f"{len(ran)} executions")
            check("the order tool executes exactly once", len(ran) <= 1,
                  f"{len(ran)} executions")
            if ran:
                payload = ran[0].get("result") or ""
                check("the broker returned an order id",
                      "orderid" in str(payload).lower(), str(payload)[:130])
            check("the approved run completes",
                  any(e["type"] == "done" for e in app_events))
            reply2 = "".join(e["delta"] for e in app_events if e["type"] == "token")
            check("the model reports the placed order", len(reply2) > 20,
                  reply2.strip()[:90].replace("\n", " "))

            # ---------- 4. the audit trail ----------
            print("\n=== audit trail ===")
            audit = http.get(f"{BASE}/api/audit", params={"limit": 30}).json()["items"]
            phases = {a["phase"] for a in audit}
            check("audit recorded decisions", "decision" in phases, str(sorted(phases)))
            approved = [a for a in audit if a["risk_verdict"] == "approved"]
            rejected = [a for a in audit if a["risk_verdict"] == "rejected"]
            check("audit recorded one approval and one rejection",
                  len(approved) >= 1 and len(rejected) >= 1,
                  f"{len(approved)} approved, {len(rejected)} rejected")
            with_ids = [a for a in audit if a.get("order_ids") not in (None, "[]", "")]
            check("audit captured an order id", len(with_ids) >= 1,
                  str(with_ids[0]["order_ids"]) if with_ids else "none")

            # ---------- 5. a stale confirm must not double-submit ----------
            print("\n=== stale confirmation ===")
            stale = http.post(f"{BASE}/api/chat/confirm", json={
                "run_id": conf2["run_id"], "session_id": conf2["session_id"],
                "decisions": {req2["id"]: True},
            })
            check("re-confirming a resolved run is refused", stale.status_code == 400,
                  f"HTTP {stale.status_code}")

            # ---------- 6. read-only questions never pause ----------
            print("\n=== read-only question ===")
            with http.stream("POST", f"{BASE}/api/chat/stream", json={
                "message": "What are my current funds?",
            }) as r:
                ro_events = read_sse(r)
            check("a read-only question never pauses",
                  not any(e["type"] == "confirm" for e in ro_events),
                  summarize(ro_events))

    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    n_pass = sum(1 for _, s in results if s == PASS)
    n_fail = sum(1 for _, s in results if s == FAIL)
    n_skip = sum(1 for _, s in results if s == SKIP)
    print("\n=== Summary ===")
    for name, status in results:
        if status == FAIL:
            print(f"  FAILED: {name}")
    print(f"  {n_pass} passed, {n_fail} failed, {n_skip} skipped")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
