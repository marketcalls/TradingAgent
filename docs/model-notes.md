# Model notes

Everything the agent knows about running on different LLMs, and the provider bugs it works
around. None of this is needed to use the app; it matters when you change `LITELLM_MODEL` or
something behaves oddly.

All of it was measured against live endpoints, not read off a docs page.

## Choosing a model

One key and one optional base URL. The prefix on `LITELLM_MODEL` picks the provider.

```bash
# Baseten
LITELLM_MODEL=baseten/deepseek-ai/DeepSeek-V4-Flash-0731
LITELLM_API_KEY=<baseten key>

# OpenAI
LITELLM_MODEL=openai/gpt-5.6-sol        # or -luna, o3, gpt-4o
LITELLM_API_KEY=sk-<openai key>

# Local, via Ollama - no key at all
LITELLM_MODEL=ollama_chat/llama3.1
LITELLM_API_KEY=
```

Anthropic, Gemini, Groq, OpenRouter and LM Studio follow the same shape.

**The agent needs tool calling.** Every answer comes from a tool, so a model that cannot call
tools is useless here.

## Measured behaviour by model

| Model | Notes |
|---|---|
| `baseten/…DeepSeek-V4-Flash` | Fast (3-4s). Reasons, but the amount cannot be controlled. Reliable at calling order tools. |
| `openai/gpt-5.6-sol`, `-luna` | Needs three workarounds, all automatic (below). Called the order tool in 5 of 6 attempts. |
| `openai/gpt-4o`, `groq/llama-3.3` | Not reasoning models; no thinking control is shown. |
| `ollama_chat/gemma4:e4b` | Works, but slow (10-30s) and weak at choosing among many tools, hence the lean profile. |

## Tool profile

`TOOL_PROFILE` is `full` (43 tools) or `lean` (15). Empty picks automatically: **lean for local
models, full otherwise**.

Small models cannot choose reliably among 43 tools. With `gemma4:e4b` on the full set, a simple
quote request produced 16 tool calls against the wrong symbol and an answer about market depth;
asked for funds, it claimed it had no such function while `get_funds` was in scope.

On lean it answers each of those in a single tool call. Lean also shortens the instruction set
and caps `tool_call_limit` at 6, so a confused model stops instead of looping.

## Thinking / reasoning effort

`LITELLM_REASONING_EFFORT` sets the default; the user can change it per message from the header,
and the choice is remembered per model. Values: `none | minimal | low | medium | high`.

The control is built from what each model actually supports, detected per model rather than per
provider - so `gpt-4o` shows no control while `o3` gets the full scale.

| Model | Control |
|---|---|
| Ollama | Off / On. LiteLLM maps effort to Ollama's boolean `think` flag, so there is no gradation |
| `openai/o3`, `anthropic/claude-sonnet-4-5` | The full scale |
| `openai/gpt-4o`, `groq/llama-3.3-70b` | None; not reasoning models |
| `baseten/…DeepSeek-V4-Flash` | None. It reasons, but the provider rejects `reasoning_effort` |

Measured on `gemma4:e4b`, same question through the full agent:

```
LITELLM_REASONING_EFFORT=          15.5s   1599 thinking characters
LITELLM_REASONING_EFFORT=none       6.7s      0 thinking characters
```

Turning thinking off made it 2.3x faster with no loss on that question. It is the single most
effective latency setting for a local model.

**If you measure this yourself, do not set `litellm.drop_params = True`.** It silently discards
`reasoning_effort`, so every level becomes the same request and any difference is noise. An
earlier version of these notes claimed DeepSeek's effort scaled its budget; that came from
exactly this mistake and was wrong.

## The reasoning-model token trap

A reasoning model spends completion tokens on hidden reasoning **before** emitting any content,
billed from the same budget as the reply. Too small a `LITELLM_MAX_TOKENS` returns an **empty**
answer rather than a short one - at `max_tokens=16` the whole budget went to reasoning and the
content came back `None` with `finish_reason="length"`.

`LITELLM_MAX_TOKENS=4096` is a correctness requirement, not a cost dial.

## Provider bugs worked around automatically

### Ollama: LiteLLM's model table is stale

LiteLLM decides Ollama tool support from a static table that does not know most tags, so it
reports `supports_function_calling=False` even for models that plainly support tools, and falls
back to a JSON-emulation path that either crashes on the turn after a tool result or returns an
empty reply. The agent asks Ollama itself (`/api/show` reports a capabilities list) and registers
the truth.

### Ollama: LiteLLM drops `tool_calls` from assistant messages

`OllamaChatConfig.transform_request` converts them, then builds the outgoing message copying only
role, thinking, content and images. Ollama therefore sees a tool result for a call it has no
record of, so the model calls the tool again, forever, with an empty final answer.

Isolated by running the identical exchange against Ollama's native `/api/chat`, which answered
correctly and called nothing. The model was never at fault. The agent restores the field.

### OpenAI gpt-5.6: three incompatibilities

1. **Tools and reasoning cannot be combined** on `/v1/chat/completions` with any effort except
   `none`, and `minimal` is rejected outright. Since this agent always sends tools, effort is
   pinned to `none` for that family and a conflicting `.env` value is overridden with a warning.
2. **`top_p` is rejected.** Sampling parameters are filtered through
   `get_supported_openai_params` and withheld when unsupported.
3. **`temperature` accepts only the value 1.** No metadata call expresses "this parameter, but
   only this value", so a small measured table carries the constraint.

### Baseten: `reasoning_effort` is rejected outright

Sending it raises `UnsupportedParamsError` and fails **every** request. The agent detects this
and does not send it.

## A model-behaviour limit worth knowing

HITL is enforced by Agno, not by the model, so nothing can reach the broker without approval.
But the gate only fires if the model actually **calls** the order tool. A model that writes the
order as a table and stops never reaches the pause, and the user is never asked.

Measured on `gpt-5.6-luna`: 5 of 6 order instructions reached the gate. Moving the rule into the
agent description (the top of the system message) took it from 2 of 3 to 5 of 6.

This is not a safety hole - nothing is placed - but it is misleading, so the backend also emits a
correction when the reply claims an order happened and no order tool ran. You will see it as a
warning under the answer.
