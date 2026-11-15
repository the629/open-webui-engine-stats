# Engine stats for Open WebUI

A single-file Open WebUI **Filter** that appends one line to each assistant message showing how the completion
actually ran:

```
⚡ prefill 1,641 tok/s (1,844 tok) · decode 60.9 tok/s (17 tok) · 0% cached · draft 81% · $0.00
```

| Field | What it tells you |
| --- | --- |
| **prefill** | prompt-processing rate and how many tokens were processed — the cost you pay before the first token |
| **decode** | generation rate and how many tokens were generated |
| **cached** | prefix-cache reuse, plus how long a restore took |
| **draft** | speculative-draft acceptance (MTP) |
| **$** | cost estimate; `$0.00` for anything not in the price table, which is what a local model gets with no configuration |

## Why not just tokens ÷ seconds?

Most token counters divide total tokens by wall-clock, which folds prompt processing and generation into one
number that describes neither. Prefill runs an order of magnitude faster than decode, so a single blended figure
can look fine while the thing you actually experience — time to first token — is terrible, or vice versa.

This reads the **engine's own timings** (`prompt_per_second`, `predicted_per_second`, `cache_n`, `draft_n`,
`prompt_n`, …) which llama.cpp and its forks already return and Open WebUI already stores on every message. It
adds no requests and no tokens: it is only displaying what is already there.

## Requirements

- **Open WebUI 0.13.0+** (built and tested on 0.13.0). Uses the filter `outlet` hook and the per-user
  `UserValves` mechanism.
- **A llama.cpp-family backend** that returns a `timings` object in the completion response — llama.cpp itself,
  plus forks built on it. Open WebUI merges `timings` into each message's `usage`, which is what this reads.

On backends that do not report timings (vLLM, Ollama, hosted APIs), the timing fields simply do not appear and
you get the cost line only. That is intentional: nothing is invented or estimated.

## Install

1. **Admin Panel → Functions → New Function** (or *Import*), and paste `context_stats.py`.
2. Enable it and make it **global** if you want it on every model.
3. **Restart Open WebUI** — functions are loaded at startup.
4. Send a message.

The file's docstring carries the metadata Open WebUI's importer reads (`title`, `version`, `type`, `active`,
`global`, `description`), so nothing else needs configuring for the default behaviour.

## Valves

| Valve | Purpose |
| --- | --- |
| `prices` | JSON, dollars per **million** tokens, per model id. Unlisted models are free — that is how local inference shows `$0.00` with no setup. Supports `in`, `out`, `cached` and `image` rates, e.g. `{"gpt-image-1": {"image": 0.04}, "some-api-model": {"in": 3.0, "cached": 0.3, "out": 15.0}}` |
| `precision` | Decimal places for a non-zero cost. Zero always renders as `$0.00`. |
| `show_prefill` / `show_decode` / `show_cache` / `show_draft` / `show_cost` | The base template: which metrics a permitted user sees unless a policy layer overrides that key. |
| `access_policy` | Who is allowed to see the line. **On by default.** See below. |
| `emit_status` | Also emit a transient status event while rendering. |
| `debug` | Log the resolved per-user settings to the container log. |

A provider that reports its own `cost` wins over the estimate, so a gateway that already knows the price is
authoritative.

## Access policy

On by default: everyone with access to the model sees the line. If you would rather keep engine internals to
yourself — reasonable on a shared box — set `access_policy` to JSON, folded **lowest layer first**, each layer
overriding only the keys it names:

```
"default"  →  matching "groups" (by id or name, in the order written)  →  "admins" (role == admin)
           →  "users" (by id or email)  →  the user's own valves
```

A layer is `true`, `false`, or an object such as `{"visible": true, "show_cost": false}`.

```json
{"default": false, "admins": true}
```

```json
{"default": false, "admins": true, "groups": {"Family": true}}
```

```json
{"default": false, "admins": true, "users": {"someone@example.com": {"visible": true, "show_cost": false}}}
```

A malformed policy **fails closed** — members off, admin on — because the wrong failure direction here leaks
engine internals to the household rather than hiding them.

Users can also set their own switches: **Controls → Valves → Select a function**. There, **Default** means
"inherit the admin policy" and **Custom** gives that user an explicit on/off for each metric.

## The part that matters: the line never reaches the model

The line is appended for display and **stripped again in `inlet` before every request**.

That is not cosmetic. An assistant message is resent on every later turn, so a decoration left in place becomes
part of the prompt — and every token of it is re-prefilled, every turn, forever, *and* it changes the prefix so
the engine's checkpoint reuse misses. Measured on the deployment this was written for: the line costs
**53 prompt tokens**, with prefix-cache reuse identical on both sides of the strip (`cache_n = 1,836` either way).

## Known limitations

- **Backend-specific**, as above. It shows what your engine reports; it does not measure anything itself.
- **Two Open WebUI valve quirks are worked around in the code**, and both are reported upstream:
  [open-webui#34474](https://github.com/open-webui/open-webui/issues/34474) (a nullable boolean valve renders as
  a text area instead of a switch — worked around with `json_schema_extra={"type": "boolean"}`) and
  [open-webui#34475](https://github.com/open-webui/open-webui/issues/34475) (the Default→Custom toggle sends an
  empty string, which fails validation — handled by a field validator). If those issues are fixed upstream, the
  workarounds become redundant but harmless. Remove them carelessly and the per-user form breaks.
- **Temporary or unsaved chats**: if Open WebUI hands the filter a message whose text lives in `output` rather
  than `content`, the line is skipped rather than risking corruption of a multimodal reply.
- **Very long single replies** are unaffected — the line is appended once, after completion.

## Tests

```sh
python3 test_context_stats.py
```

59 checks, standard library only, no Open WebUI and no pydantic required — a two-class stub stands in for
pydantic so the rendering, cost arithmetic, strip round-trip, and the whole access-policy resolver can be
exercised anywhere, including Windows. That is deliberate: a function that silently stops rendering is the
failure mode worth guarding against, and it should be testable without standing up a container.

## Licence

MIT — see [LICENSE](LICENSE).
