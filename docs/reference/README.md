# Reference material

Every source consulted while writing `docs/plan/PLAN.md`, copied here so the plan can be
audited without the original folders.

| Folder | Origin | Contents |
|---|---|---|
| `openalgo-api/` | `openalgo/docs/api/` | All 53 v1 REST endpoint pages, the endpoint README, rate limiting. Covers order management, order and account information, market data, symbol services, options analytics, market calendar, analyzer, chart, utility, Telegram, WhatsApp, and the WebSocket protocol. |
| `openalgo-prompt/` | `openalgo/docs/prompt/` | `symbol-format.md`, `order-constants.md`, `openalgo python sdk.md`, and `indicators/` - all nine indicator pages (introduction, trend, momentum, volatility, volume, oscillators/statistical, hybrid, utility, flow indicator reference). |
| `agno/` | `Day08/agno docs` | The Agno documentation tree. The 12 MB `examples/` subtree was not copied; nothing in the plan depends on it. Most relevant here: `hitl/`, `tools/`, `sessions/`, `state/`, `models/providers/gateways/litellm/`, `run-cancellation/`. |
| `reference-app/` | `Day08/equity-research-agent` | That project's `PLAN.md` and `README.md`. The new app mirrors its FastAPI + SSE + Agno + React architecture. |
| `research-notes/` | produced for this plan | Five verification notes written by reading and **executing** the installed packages, not the docs. |

## The research notes

These are the authoritative detail behind Part 1 of the plan. Where the vendor docs and the
installed source disagree, these notes record the disagreement and the source wins.

| Note | What it establishes |
|---|---|
| `notes-indicators.md` | The real `ta` surface: 127 callables, exact signatures, input series, output tuple names, warm-up lengths, and 14 documented-versus-actual discrepancies. Includes the two genuinely broken indicators in openalgo 2.0.3. |
| `notes-api.md` | Full 57-endpoint inventory with parameters and validation rules, every constant list, the destructive-endpoint set that drives the confirmation gate, and six registered-but-undocumented routes. |
| `notes-sdk.md` | The installed `openalgo` 2.0.3 client: mixin composition, 53 public members with exact signatures, the sync-only threading consequence, the `price_type`/`pricetype` split, and the 13 endpoints the SDK does not wrap. |
| `notes-agno.md` | Agno 2.8.7: the human-in-the-loop confirmation API (`RunRequirement`, `confirm`/`reject`, `continue_run(requirements=...)`), the full streaming event list, toolkit registration and schema-generation rules, and the 2.8.7 deprecations the shipped docs still show. |
| `notes-reference-app.md` | The equity-research-agent's real structure, its SSE event contract, exact frontend dependency versions, and its PLAN.md heading outline. |

## Versions these notes describe

`agno` 2.8.7, `litellm` 1.79.1, `openalgo` 2.0.3, `httpx` 0.28.1, on Python 3.14.
Re-verify before trusting any signature after an upgrade; the plan's Step 5 startup assertion
exists for exactly this reason.
