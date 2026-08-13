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


def prepare_provider(model_id: str, api_base: str | None = None) -> None:
    """Apply any provider-specific corrections needed before building the model."""
    if model_id.startswith(("ollama/", "ollama_chat/")):
        register_ollama_tool_support(model_id, api_base)
