"""The confirmation gate must hold on EVERY model, not just the configured one.

HITL is enforced by Agno rather than by the model, so in principle it cannot be talked
around. In practice the gate only fires if the model actually CALLS the order tool - a
model that describes the order in prose instead never reaches the pause, and the user
sees no approval card. That failure looks identical to a broken gate, so it is worth
proving per model.

Each model is checked for four things:
  1. proposing an order PAUSES the run,
  2. the pending tool is the order tool, with the right arguments,
  3. the broker was NOT touched before approval,
  4. approving resumes and executes exactly once.

Models that cannot be reached are skipped loudly rather than silently passing.

Safety: refuses to run unless OpenAlgo reports analyzer mode, so every order is
simulated. Uses CNC because the sandbox rejects MIS after 15:15 IST.

Run:  python backend/tests/test_hitl_models.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import Settings, setup_logging  # noqa: E402

setup_logging("ERROR")

from app.agent import build_agent  # noqa: E402
from app.openalgo.client import get_client  # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str]] = []

# A distinct order per model. RiskGuard is a process-level singleton built from
# get_settings(), so a per-model Settings copy cannot disable its duplicate window -
# three identical orders in a row would be refused as duplicates and the execution leg
# would prove nothing.
ORDERS = {
    "ollama": ("Buy 1 share of SBIN on NSE at market price, CNC product.", "SBIN"),
    "baseten": ("Buy 2 shares of INFY on NSE at market price, CNC product.", "INFY"),
    "openai": ("Buy 3 shares of TCS on NSE at market price, CNC product.", "TCS"),
}
DEFAULT_ORDER = ("Buy 1 share of SBIN on NSE at market price, CNC product.", "SBIN")
ORDER_TOOLS = {"place_order", "place_smart_order"}


def check(name: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    results.append((name, status))
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    results.append((name, SKIP))
    print(f"  [{SKIP}] {name}" + (f" - {detail}" if detail else ""))


def models_to_test() -> list[tuple[str, str]]:
    """(model_id, api_key). A key of "" means the model needs none."""
    base = Settings.load()
    out: list[tuple[str, str]] = [("ollama_chat/gemma4:e4b", "")]

    # The configured key belongs to whichever provider LITELLM_MODEL names.
    if base.litellm_api_key and base.model_provider == "baseten":
        out.append(("baseten/deepseek-ai/DeepSeek-V4-Flash-0731", base.litellm_api_key))
    elif base.litellm_api_key and base.model_provider == "openai":
        out.append(("openai/gpt-5.6-luna", base.litellm_api_key))

    # Anything else has to come from the environment explicitly.
    for env_var, model in (("OPENAI_TEST_KEY", "openai/gpt-5.6-luna"),
                           ("BASETEN_TEST_KEY",
                            "baseten/deepseek-ai/DeepSeek-V4-Flash-0731")):
        key = os.getenv(env_var, "")
        if key and not any(m == model for m, _ in out):
            out.append((model, key))
    return out


def exercise(model_id: str, api_key: str) -> None:
    print(f"\n=== {model_id} ===")
    settings = Settings.load()
    settings.litellm_model = model_id
    settings.trading_enabled = True
    settings.require_analyzer_mode = True
    settings.duplicate_order_window_sec = 0
    if api_key:
        settings.litellm_api_key = api_key

    try:
        agent = build_agent(settings)
    except Exception as exc:  # noqa: BLE001
        skip(f"{model_id}: build", f"{type(exc).__name__}: {str(exc)[:80]}")
        return

    provider = model_id.split("/", 1)[0].replace("_chat", "")
    prompt, expect_symbol = ORDERS.get(provider, DEFAULT_ORDER)

    try:
        t0 = time.time()
        run = agent.run(prompt, session_state={"trading_enabled": True})
    except Exception as exc:  # noqa: BLE001
        skip(f"{model_id}: unreachable", f"{type(exc).__name__}: {str(exc)[:90]}")
        return

    paused = bool(getattr(run, "is_paused", False))
    reqs = list(getattr(run, "active_requirements", []) or [])
    check(f"{model_id}: proposing an order pauses the run", paused and bool(reqs),
          f"is_paused={paused} requirements={len(reqs)} in {time.time() - t0:.1f}s")
    if not (paused and reqs):
        content = " ".join((run.content or "").split())[:150]
        called = [t.tool_name for t in (run.tools or [])]
        print(f"       tools called: {called}")
        print(f"       said instead: {content}")
        return

    te = reqs[0].tool_execution
    check(f"{model_id}: the pending tool is an order tool",
          te.tool_name in ORDER_TOOLS, str(te.tool_name))
    args = te.tool_args or {}
    check(f"{model_id}: arguments carried through",
          str(args.get("symbol", "")).upper() == expect_symbol,
          str({k: args.get(k) for k in ("symbol", "action", "quantity", "product")}))

    executed_before = [t for t in (run.tools or [])
                       if t.tool_name in ORDER_TOOLS and t.result]
    check(f"{model_id}: broker NOT touched before approval", not executed_before,
          f"{len(executed_before)} executions")

    # approve, in a resumed run, exactly as the confirm endpoint does
    try:
        for req in reqs:
            if req.needs_confirmation:
                req.confirm()
        resumed = agent.continue_run(run_id=run.run_id, session_id=run.session_id,
                                     requirements=run.requirements)
        ran = [t for t in (resumed.tools or []) if t.tool_name in ORDER_TOOLS]
        # A refusal is a call that did not throw, so "did not error" is not enough:
        # the broker must actually have returned an order id.
        payload = str(ran[0].result) if ran else ""
        placed = len(ran) == 1 and not ran[0].tool_call_error and "orderid" in payload
        check(f"{model_id}: approval places the order exactly once", placed,
              payload[:110] if payload else "no execution")
    except Exception as exc:  # noqa: BLE001
        check(f"{model_id}: approval executes exactly once", False,
              f"{type(exc).__name__}: {str(exc)[:90]}")


def main() -> int:
    print("HITL across models")
    client = get_client()
    if not client.settings.openalgo_api_key or not client.ping().get("ok"):
        skip("all checks", "OpenAlgo unreachable")
        return 0
    mode = client.analyzer_mode()
    if mode != "analyze":
        skip("all checks", f"OpenAlgo is in {mode} mode; refusing to place real orders")
        return 0
    print(f"  broker reachable, analyzer mode = {mode}")

    targets = models_to_test()
    print(f"  models under test: {[m for m, _ in targets]}")
    for model_id, key in targets:
        exercise(model_id, key)

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
