# Prompts

Every instruction typed by the user during this build, verbatim and in order. Kept as a record
of how the project was actually specified: the scope arrived in pieces, and several late
messages changed decisions that had already been made.

---

## 1. The initial brief

> I want to build a simple Trading Agent (Chat) similar & 'd:\AI Bootcamp 2026\Day08\equity-research-agent' using Agno (reference & 'd:\AI Bootcamp 2026\Day08\agno docs') and use openalgo api and openalgo sdk to build the AI Agent it supports all trading functions, indicator functions, accounts, data. It also supports 100+ indicators. & 'd:\AI Bootcamp 2026\Day08\openalgo\docs\api'  & 'd:\AI Bootcamp 2026\Day08\openalgo\docs\prompt\symbol-format.md' & 'd:\AI Bootcamp 2026\Day08\openalgo\docs\prompt\order-constants.md' & 'd:\AI Bootcamp 2026\Day08\openalgo\docs\prompt\openalgo python sdk.md' . Ensure AI agents supports all the endpoints, all the indicators and build a very comprehensive Trading Agent which i can chat and execute stuff. explain your plan in .md format & 'd:\AI Bootcamp 2026\Day08\TradingAgent\docs\plan'

Produced the reference research and `docs/plan/PLAN.md`.

---

## 2. Missing docs, and parallel research

> i missed the indicator docs & 'd:\AI Bootcamp 2026\Day08\openalgo\docs\prompt\indicators'. spin multiple agents to prepare the plan

Five research agents ran in parallel over the indicator docs, the REST API docs, the installed
SDK source, the Agno docs and the reference app. Their notes are in
`docs/reference/research-notes/`. This is where the real indicator count of 127 came from, along
with the finding that several documented signatures were wrong.

---

## 3. Copy the reference material into the project

> copy all the docs which you are using for reference & 'd:\AI Bootcamp 2026\Day08\TradingAgent\docs\reference' here

Copied into `docs/reference/`. The third-party trees (Agno's docs, OpenAlgo's API and prompt
docs, the reference app) were later gitignored rather than republished under this repository's
MIT licence; only the research notes written for this project are committed. See
`docs/reference/README.md`.

---

## 4. Switch the model path to LiteLLM and Baseten

> use litellm and use baseten as a inference provider and here is a simple example to use deepseek v4 flash model using baseten as inference # You can use this model with any of the OpenAI clients in any language!
> \# Simply change the API Key to get started
>
> ```python
> from openai import OpenAI
>
> client = OpenAI(
>     api_key="BASETEN_API_KEY",
>     base_url="https://inference.baseten.co/v1"
> )
>
> response = client.chat.completions.create(
>     model="deepseek-ai/DeepSeek-V4-Flash-0731",
>     messages=[
>         {
>             "role": "user",
>             "content": "Implement Hello World in Python"
>         }
>     ],
>     stream=True,
>     stream_options={
>         "include_usage": True,
>         "continuous_usage_stats": True
>     },
>     top_p=1,
>     max_tokens=1000,
>     temperature=1,
>     presence_penalty=0,
>     frequency_penalty=0
> )
>
> print(response.choices[0].message.content)
> ```
>
> . Use the litellm only. also prepare the .env where i will upload my keys including openalgo

Resolved to LiteLLM's first-class `baseten` provider rather than the generic OpenAI-compatible
route, and produced `.env.example`. Rewrote Part 1.2 of the plan.

---

## 5. Test the keys

> can you test the keys from openalgo and litellm (baseten) check whether the models are working or not and check the openalgo quotes

Produced `scripts/validate_setup.py`, 26 live checks. This run surfaced the three findings that
shaped the rest of the build: DeepSeek V4 Flash is a reasoning model whose small `max_tokens`
returns empty content rather than truncated content; the OpenAlgo SDK's `_make_request`
308-redirects on a leading slash; and `history` returns six columns, not the documented five.

---

## 6. Start building, in parallel

> start building the Trading Agent by spinning up multiple agents.

---

## 7. Build in a loop, publish to GitHub

> build this in a loop and ensure that you are validating , commit and push to the newer github project - TradingAgent. Keep it in public mode, MIT License

Superseded moments later by:

> build this in a loop and ensure that you are validating , commit and push to the newer github project - TradingAgent. Keep it in public mode, MIT License, use a sleep time of 2mins

Created https://github.com/marketcalls/TradingAgent (public, MIT) and a 2-minute build loop.

---

## 8. Document each push

> after every commit and push ensure you explain the progress as a seperate .md file in the & 'd:\AI Bootcamp 2026\Day08\TradingAgent\docs\progress'

Produced `docs/progress/001` through `008`, each recording what was built, the real validation
output, and what came next.

---

## 9. This file

> all the prompt i typed push it to prompt.md & 'd:\AI Bootcamp 2026\Day08\TradingAgent\docs\prompt'

---

## Appendix: the loop prompt

Not typed by the user directly. Derived from message 7 and fired every two minutes until all 12
build steps were done:

> Continue building the OpenAlgo Trading Agent per docs/plan/PLAN.md. Each iteration: implement the next unfinished step from the plan's Part 9 build order, validate it by actually running it (unit test or live smoke test against the running OpenAlgo instance), then commit and push to origin/main, then write a numbered progress .md into docs/progress/ describing what was built, what was validated with real output, and what is next. Never commit .env or secrets. Stop the loop when all 12 build steps are done and the app runs end to end.
