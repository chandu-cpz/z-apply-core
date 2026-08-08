# Plan: Sequential resolution/fill + tolerance for false no-progress stalls

Status: proposal for user review.

## Background

Run against `https://www.lifeatvena.com/postings/81985e3b-a892-4fe1-b0b8-958f7139f973/applications/new`
was making real progress (auth verified, resume attached, many AnswerWriter
answers) but was cut off at:

```
orchestrator recovery started attempt 2: Repeated denied or non-progress tool calls (browser_click) ended this agent turn
```

Every evidence point of progress was real: `browser_revision` advanced, fields
held values, radios were flipped. The stall was a false positive caused by the
runtime, not by a stalled agent.

## Root causes found in run `c23f7cebbd5e41c9974142447bb8f568`

1. **Agent passes `ref` to `browser_click`; the pydantic args schema forbids it.**
   - Model emits e.g. `browser_click({"element":"img","ref":"e234","target":"e234"})`.
   - `_tool_model()` in `src/z_apply_core/browser_tools.py:445-464` builds the schema with
     `ConfigDict(extra="forbid")`, and the backend `browser_click` schema
     (`playwright-python-mcp/backend/tools/snapshot.py:104`) has no `ref`
     parameter.
   - Result: pydantic `extra_forbidden` validation failure makes every such
     click a `status=error` tool result.
2. **Every error-status result feeds the non-progress circuit.**
   - `src/z_apply_core/agents/no_progress_guard.py:300-303`: `_is_non_progress()`
     returns True for ANY error ToolMessage.
   - `max_non_progress=3` (line 50) and `max_identical_denials=2` (line 49).
   - A single batched response containing several `browser_click` calls
     (radios e179/e192/e217/e243), all rejected for `ref`, trips the circuit
     even when the page continued advancing.
   - Error message is attached at lines 200-228.
3. **`normalize_browser_arguments` keeps `ref` in the payload (never strips it).**
   - `src/z_apply_core/browser_tools.py:134-160` copies arguments verbatim and
     only rewrites `target`; a stray `ref` is never dropped, so the schema
     rejection is not preventable by normalization.
4. **AnswerWriter gets combobox tasks that need in-browser observation but the
   subagent has no browser.**
   - The orchestrator prompt currently tells AnswerWriter "first observe the
     available options by opening the combobox" (the combobox tasks dispatched
     for laf location / notice period did this) — but AnswerWriter can't touch
     the browser, so it resorts to `glob`/`grep`/`read_file` with no results and
     times out (`RuntimeError: Timed out`).
   - Multiple fields "Timed out" this way.
5. **The orchestrator prompt explicitly demands everything concurrently.**
   - `prompts/orchestrator.md` step 6: "dispatch ONE `task` call ... for every
     visible field — emit ALL of those task calls in the SAME response so they
     run concurrently".  It also suggests batch plain textboxes into ONE
   `browser_fill_form`.

## User-chosen direction (this plan)

1. **Sequential resolution & sequential fill.** Orchestrator resolves one field
   one at a time (single AnswerWriter task per response, with exact field
   evidence), then fills one control at a time (each mutation in its own
   response + its own `browser_wait_for`/receipt verification) instead of a
   concurrent bulk dispatch.

2. **Allow `ref` in click/select schemas (or strip unknown args).** Choices:
   - `browser_tools.py`: in `_tool_model`, tolerate `ref` and drop it (or map
     it into `element`/`target`) so `browser_click({"target":"e234","ref":"e234"})`
     never becomes a validation failure. Recommendation: accept & drop `ref`
     keys, so the browser layer of spec at snapshot.py still receives only
     valid fields.
   - Alternatively export a normalized args layer that filters the passed
     kwargs to the backend spec's declared parameters.

3. **Non-progress guard: ignore repeated tool-validation errors from within
   one turn** (they only prove arg sloppiness, not a stalled loop). If we still
   want to count them, raise `max_non_progress` for the orchestrator guard (or
   exclude `browser_click`/`browser_select_option` names from the circuit when
   they fail only validation; the narrower fix is to keep counting only
   runtime-browser errors).

4. **Only fill with a smaller number of fields.** Change orchestrator prompt to
   dispatch candidate-field resolution sequentially and fill each successive
   control one at a time, verifying with the suffix receipts.

## Files to change (proposal)

- `src/z_apply_core/browser_tools.py`
  - `_tool_model`: allow lightweight extra-field tolerance for `ref`/
    element-naming close synonyms (or strip keys not declared by backend spec).
  - `normalize_browser_arguments`: drop unknown fields (`ref`) instead of
    carrying them to the executor.
- `src/z_apply_core/agents/prompts/orchestrator.md`
  - Replace the "dispatch all AnswerWriter tasks in the same response" rule
    with sequential-resolution rule + per-turn verification.
  - Step 6 (combobox) similarly must stay single-mutation per response and
    sequential.
  - Step 7: fill one control per response (not one big `browser_fill_form` for
    all plain inputs) — planned user choice.
- `src/z_apply_core/agents/no_progress_guard.py`
  - Optionally: count only a `browser_observe` signature after batching, or
    add `max_turn_validation_errors`/`allow_schema_retry` knob so the stale
    `ref` clicks don't flip the circuit.
- Possibly `src/z_apply_core/agents/subagent_dispatch.py` to keep a
  combobox-vs-no-browser check: never dispatch a combobox task asking
  "observe the options by opening the combobox" — instead the orchestrator
  opens it itself and hands over the observed options.
- `AGENTS.md` (`/home/chandu/z-apply/AGENTS.md:62-67`): currently documents
  "Candidate-field resolution is parallel … Do not reintroduce a serialization
  gate that truncates an all-candidate-resolution batch." This plan reverses
  that: resolution becomes sequential (one `task` per response). The AGENTS.md
  fact and the prompt docs must change together so the repo stays factual.

## Validation

- Run a synthetic single-turn check that `browser_click({"ref":"e234",...})`
  is no longer rejected by the args schema.
- Perform the run shown above, ensuring: sequential resolution, sequential
  fill, no `Repeated denied or non-progress tool calls` false positive on a
  healthy turn.

## Open questions for user

- Sequential fill for plain text inputs calls more `browser_fill_form` calls
  (cost). Do you want SEQUENTIAL for non-input controls only and keep batch per
  field-type text fills, or strictly every control one at a time?
- Add a `max_turn_validation_errors` if we intend to only block on errors after
  an actual browser check? (recommended)
