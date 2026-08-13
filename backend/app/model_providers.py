"""Provider quirks that have to be corrected before the model is built.

Right now that means one thing: teaching LiteLLM which local Ollama models can call
tools.

The problem, measured against litellm 1.79.1 and ollama with gemma4:e4b:

  LiteLLM decides whether an Ollama model supports tool calling by looking it up in a
  STATIC table (litellm.get_model_info). Most Ollama tags are not in that table, so the
  lookup says supports_function_calling=False even for models that plainly do support
  tools. LiteLLM then falls back to a legacy emulation path in OllamaChatConfig:

      optional_params["format"] = "json"
      optional_params["functions_unsupported_model"] = tools
      if len(tools) == 1:
          optional_params["function_name"] = tools[0]["function"]["name"]

  That has two failure modes, both of which this agent hits:

    - With exactly ONE tool, transform_response takes the branch guarded by
      `format == "json" and function_name is not None` and runs
      json.loads(message["content"]). On the follow-up turn after a tool result the
      model returns an empty string, so it raises
      "APIConnectionError: Expecting value: line 1 column 1 (char 0)".

    - With MANY tools there is no crash, but format="json" still forces the model to
      emit JSON only, so the final prose answer comes back EMPTY.

Ollama itself knows the truth: /api/show reports a capabilities list containing "tools"
for models that support them. So we ask Ollama and register the answer with LiteLLM,
which makes it use the native tool-calling path instead of the emulation.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

DEFAULT_OLLAMA_BASE = "http://localhost:11434"


def ollama_model_capabilities(model_tag: str, api_base: str | None = None,
                              timeout: float = 5.0) -> list[str]:
    """Ask Ollama what a model can do. Returns [] if it cannot be reached."""
    base = (api_base or DEFAULT_OLLAMA_BASE).rstrip("/")
    # LiteLLM accepts a base with or without /v1; Ollama's native API is at the root.
    if base.endswith("/v1"):
        base = base[:-3]
    try:
        r = httpx.post(f"{base}/api/show", json={"model": model_tag}, timeout=timeout)
        r.raise_for_status()
        return list(r.json().get("capabilities") or [])
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read ollama capabilities for %s: %s", model_tag, exc)
        return []


def register_ollama_tool_support(model_id: str, api_base: str | None = None) -> bool:
    """Register a local Ollama model with LiteLLM as tool-capable, if it is.

    Args:
        model_id: The full LiteLLM id, e.g. "ollama_chat/gemma4:e4b".
        api_base: Optional Ollama base URL.

    Returns:
        True if the model reports tool support and was registered.
    """
    if "/" not in model_id:
        return False
    prefix, tag = model_id.split("/", 1)
    if prefix not in ("ollama", "ollama_chat"):
        return False

    capabilities = ollama_model_capabilities(tag, api_base)
    if "tools" not in capabilities:
        log.warning(
            "ollama model %s does not report tool support (capabilities=%s). This agent "
            "needs tool calling; pick a model that has it, such as llama3.1 or qwen3.",
            tag, capabilities or "unknown",
        )
        return False

    try:
        import litellm
        # get_model_info is called as (model=tag, custom_llm_provider="ollama"), which
        # resolves the key "ollama/<tag>". Register both spellings so either prefix works.
        entry = {
            "litellm_provider": "ollama",
            "mode": "chat",
            "supports_function_calling": True,
        }
        litellm.register_model({f"ollama/{tag}": dict(entry),
                                f"ollama_chat/{tag}": dict(entry)})
        log.info("registered ollama model %s as tool-capable (capabilities=%s)",
                 tag, capabilities)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("could not register ollama model %s with litellm: %s", tag, exc)
        return False


def patch_ollama_tool_call_roundtrip() -> bool:
    """Work around a LiteLLM bug that breaks the tool-result round trip on Ollama.

    In litellm 1.79.1, OllamaChatConfig.transform_request converts each assistant
    message's tool_calls into Ollama's shape:

        cast(dict, m)["tool_calls"] = new_tools

    but then builds the message it actually sends from scratch and copies only role,
    thinking, content and images:

        ollama_message = OllamaChatCompletionMessage(role=...)
        ... thinking / content / images ...
        new_messages.append(ollama_message)

    tool_calls is never copied across. So on the turn after a tool runs, Ollama receives
    an assistant message with no record of having called anything, followed by a tool
    result for a call it cannot see. The model has no choice but to call the tool again,
    which shows up as an endless loop and an empty final answer.

    Verified against gemma4:e4b: through LiteLLM the follow-up turn re-called get_funds
    and returned empty content, while the identical exchange against Ollama's native
    /api/chat answered "You have an available balance of 9,999,977.02" and called
    nothing. The model was never the problem.

    This copies the already-converted tool_calls onto the outgoing message. It is
    idempotent and a no-op if LiteLLM fixes the bug and starts sending them itself.
    """
    try:
        from litellm.llms.ollama.chat.transformation import OllamaChatConfig
    except Exception:  # noqa: BLE001
        return False

    if getattr(OllamaChatConfig, "_oa_tool_calls_patched", False):
        return True

    original = OllamaChatConfig.transform_request

    def transform_request(self, model, messages, optional_params, litellm_params, headers):
        # The original mutates each source message in place, replacing tool_calls with
        # the Ollama-shaped list, so after this call the converted form is available.
        data = original(self, model=model, messages=messages,
                        optional_params=optional_params,
                        litellm_params=litellm_params, headers=headers)
        try:
            outgoing = data.get("messages") or []
            for src, dst in zip(messages, outgoing):
                if not isinstance(dst, dict) or dst.get("tool_calls"):
                    continue
                if hasattr(src, "model_dump"):
                    src = src.model_dump(exclude_none=True)
                if not isinstance(src, dict):
                    continue
                calls = src.get("tool_calls")
                if calls:
                    dst["tool_calls"] = calls
        except Exception:  # noqa: BLE001
            log.warning("could not restore ollama tool_calls", exc_info=True)
        return data

    OllamaChatConfig.transform_request = transform_request
    OllamaChatConfig._oa_tool_calls_patched = True
    log.info("patched litellm OllamaChatConfig to send assistant tool_calls")
    return True


# Models whose real behaviour was MEASURED here and disagrees with LiteLLM's table.
# Keyed by a substring of the model id. Keep this short and evidence-based.
_MEASURED_REASONING: dict[str, dict] = {
    # litellm.supports_reasoning() returns False for this, but it demonstrably reasons:
    # reasoning_content comes back populated and usage reports reasoning_tokens
    # (202 at default effort, 1283 at low, 1498 at high). "none" does not disable it.
    "deepseek-v4-flash": {"thinks": True, "graded": True, "can_disable": False,
                          "note": "Measured: this model always reasons. Effort scales the "
                                  "budget but cannot switch it off. A higher effort needs "
                                  "a higher max_tokens or the reply can come back empty."},
}


def _measured_override(model_id: str) -> dict | None:
    lowered = model_id.lower()
    for needle, data in _MEASURED_REASONING.items():
        if needle in lowered:
            return data
    return None


def detect_reasoning_support(model_id: str, api_base: str | None = None) -> dict:
    """Work out whether THIS model can think, and how much control it offers.

    Detection is dynamic, not a provider table, because it varies per model within a
    provider: openai/gpt-4o cannot reason while openai/o3 can, and the UI should offer
    no thinking control at all for the former.

    Sources, in order:
      1. Ollama - ask the server. /api/show lists a capabilities array, and "thinking"
         appears only on models that support it. Authoritative and always current.
      2. A short table of models measured directly in this project, where LiteLLM's
         metadata is wrong.
      3. litellm.supports_reasoning(), plus whether reasoning_effort appears in
         get_supported_openai_params, which distinguishes a graded control from an
         on/off one.
    """
    provider = model_id.split("/", 1)[0].lower() if "/" in model_id else ""

    if provider in ("ollama", "ollama_chat"):
        tag = model_id.split("/", 1)[1]
        caps = ollama_model_capabilities(tag, api_base)
        if not caps:
            return {"thinks": False, "graded": False, "can_disable": False,
                    "verified": False, "detected_by": "ollama (unreachable)",
                    "note": f"Could not reach Ollama to ask what {tag} supports."}
        thinks = "thinking" in caps
        return {
            "thinks": thinks,
            # LiteLLM maps reasoning_effort onto Ollama's boolean `think` flag.
            "graded": False,
            "can_disable": thinks,
            "verified": True,
            "detected_by": "ollama /api/show",
            "note": ("Ollama exposes thinking as on or off, with no gradation."
                     if thinks else
                     f"{tag} does not report a thinking capability, so there is nothing "
                     "to control."),
        }

    measured = _measured_override(model_id)
    if measured:
        return {**measured, "verified": True, "detected_by": "measured in this project"}

    try:
        import litellm
        thinks = bool(litellm.supports_reasoning(model=model_id))
        graded = False
        if thinks:
            try:
                params = litellm.get_supported_openai_params(model=model_id) or []
                graded = "reasoning_effort" in params
            except Exception:  # noqa: BLE001
                graded = False
        return {
            "thinks": thinks,
            "graded": graded,
            "can_disable": graded,
            "verified": False,
            "detected_by": "litellm.supports_reasoning",
            "note": ("" if thinks else
                     f"{model_id} is not a reasoning model, so there is no thinking to "
                     "control."),
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("could not detect reasoning support for %s: %s", model_id, exc)
        return {"thinks": False, "graded": False, "can_disable": False,
                "verified": False, "detected_by": "unavailable",
                "note": "Could not determine whether this model reasons."}


def prepare_provider(model_id: str, api_base: str | None = None) -> None:
    """Apply any provider-specific corrections needed before building the model."""
    if model_id.startswith(("ollama/", "ollama_chat/")):
        register_ollama_tool_support(model_id, api_base)
        patch_ollama_tool_call_roundtrip()
