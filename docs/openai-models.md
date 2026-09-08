# Using OpenAI (and other hosted) models

Otto talks to any OpenAI-compatible endpoint: configure it once under Admin → LLM models, and
every model on it inherits the URL and key. `api.openai.com/v1` is just another endpoint.

Four things differ **between models on the same endpoint**, and every one of them bit in
production (issue #10). Three Otto now handles by itself, learned from the server's own 400. The
fourth decides which models you can use for execution at all, and no amount of code can work
around it — so check before you assign.

## Check before you assign

```
./.venv/bin/python probe_endpoint.py             # every configured endpoint
./.venv/bin/python probe_endpoint.py OpenAI      # one, by endpoint or model name
```

Three tiny calls per model. Read-only — it never writes config. Run it when you add an endpoint,
and again whenever a model generation ships; the answers change with each one.

## 1. `max_tokens` was renamed — handled

From the gpt-5 generation on, the output budget is `max_completion_tokens` and the old name is a
400. **You do not need to configure anything.** Otto reads the rename off the server's own error,
retries, and remembers it on the model entry as `quirks: ["max_completion_tokens"]`, so it is
paid once ever rather than per call.

## 2. `temperature: 0` is refused — handled

Otto sends `temperature: 0` for determinism. Most newer models accept only their default and 400
on anything else. Same mechanism, same one-off cost, `quirks: ["default_temperature"]`.

Not generational, so don't guess: `gpt-5.1`, `gpt-5.2` and the `gpt-5.4` family accept `0` while
`gpt-5`, `gpt-5.5` and the `o`-series do not.

## 3. Function tools on `/chat/completions` — NOT handled, and cannot be

**This is the one that matters when picking an execution model.** Otto's local backend
(`local_runtime.py`) is a tool-calling loop, so a model that cannot take `tools` on
`/chat/completions` can never be an execution model, however capable it is.

OpenAI's newest generation refuses:

> Function tools with reasoning_effort are not supported for gpt-6-astra in
> /v1/chat/completions. To use function tools, use /v1/responses or set reasoning_effort to
> 'none'.

Read that carefully, because both halves mislead:

- **It names `reasoning_effort`, but the parameter is not the problem.** The same 400 comes back
  with no `reasoning_effort` in the body at all. Measured, not inferred.
- **Its suggested remedy does not work.** Setting `'none'` is refused by the same model
  (*"Supported values are: 'low', 'medium', 'high', and 'xhigh'"*). Obeying it made Otto's
  adaptation two-directional and the request oscillated until the round budget died — which is
  why every adaptation in `gateway.adapt_body` must now be monotone.

So Otto treats it as what it is: the existing `tools_unsupported` wall. The run re-dispatches to
Claude in one round trip and says which model to change. `otto doctor`'s **exec tool calls**
check reports the same thing before a run pays for it.

A blocked model is still perfectly good for the **tool-free** tiers — routing, clarify, verify,
memory — and for tool-free read capabilities via `cap_local_exec`. Only execution is off limits.

## 4. The output budget is one number for every model — handled

`config.LOCAL_EXEC_MAX_TOKENS` (32768) is asked for regardless of what the model can emit, so
gpt-4o (16384), gpt-4 and gpt-3.5-turbo rejected *every* call:

> max_tokens is too large: 32768. This model supports at most 16384 completion tokens.

Not a context overflow — the prompt fits fine. Otto clamps to the ceiling the server names, in
one round trip rather than halving toward it.

## Snapshot — `api.openai.com`, 2026-09-08

Refresh with `probe_endpoint.py` rather than trusting these rows; they are dated for a reason.

| tools on `/chat/completions` | models |
| --- | --- |
| **works** (31 of 38, incl. gpt-4o/gpt-4/gpt-3.5 once the output budget is clamped) | `gpt-3.5-turbo`, `gpt-4`, `gpt-4-turbo`, `gpt-4.1{,-mini,-nano}`, `gpt-4o{,-mini}`, `gpt-5{,-mini,-nano}`, `gpt-5.1`, `gpt-5.2`, `gpt-5.4{,-mini,-nano}`, `gpt-5.5`, `o1`, `o3`, `o3-mini`, `o4-mini` |
| **refuses tools** | `gpt-5.6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-6-astra` |
| **no `/chat/completions` at all** | `gpt-5-pro`, `gpt-5.3-codex`, `o1-pro` |

At that date the newest model that can drive execution is **`gpt-5.5`**; `gpt-5.4` also accepts
`temperature: 0`, so it needs one quirk instead of two.

## Tell Otto the endpoint is hosted

An endpoint has a **kind**: `local` (a model on a box you run) or `hosted` (a frontier model
behind a vendor API). Every model on the endpoint shares it, and it is what the weak-model
safeguards key on — the write latch that escalates a verify-failed write to Claude, the
cross-run capability latch, and the "fix the local endpoint" wording in strict-mode stops and
needs-you cards. A `hosted` model gets none of those: it retries like any strong model, and a
failure names the endpoint.

Well-known vendor hosts (`api.openai.com`, `openrouter.ai`, …) are pre-selected `hosted` when
the endpoint is created; anything else defaults to `local`. Set it yourself in Admin → LLM models
→ endpoint → edit. A config saved before this field existed is classed on first load by the same
host list.

What the kind does **not** change: the approval preview. It runs `claude -p --permission-mode
plan`, so it is Claude-only whatever the endpoint is, and a non-Claude pick there is repointed
to sonnet with the radio disabled.

## Not supported: the Responses API

`/v1/responses` is where OpenAI moved function calling for the newest models. Otto speaks
`/chat/completions` only. Supporting it is a feature, not a fix — a second wire format in
`local_runtime` and `gateway` — so it belongs in its own issue rather than being bolted onto the
parameter-dialect work.

## Where this lives in the code

`gateway.chat_body` is the ONE place a `/chat/completions` body is built, so a dialect learned on
any path is known to all of them; `error_classifier.param_quirk` decides what a 400 is asking
for. Both carry the invariants — see `.claude/rules/gateway-backends.md`.
