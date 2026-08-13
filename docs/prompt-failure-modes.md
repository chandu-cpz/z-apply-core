# Prompt failure-mode analysis (from run traces)

Source: `.z-apply/runs/*/call-ledger.json` + `context/obs_*.txt` of the 8 real
blocked/failed runs (greenhouse, keka, freshteam, workable). This is the
evidence base for prompt iterations — each mode names the concrete page state
the agent could not escape.

## FM-1 — Simplify-proxy page recognition (keka) — worst offender

- Run `24e647a3` (default prompt): **62 calls, 1.09M input tokens, $0.025**,
  blocked with ~7 required fields still unresolved.
- Page: `toprankers.keka.com/careers/applyjob/155148` renders the *employer
  form* under the page title **"Simplify Jobs | Dashboard"** (the Simplify
  extension's proxy). The agent's evidence shows a full keka form (resume
  attached, First/Last name, phone `+919063812386`, email as
  `<secret>DEFAULT_USERNAME</secret>`, employer/education rows filled), yet
  the run still blocked with: **Gender combobox ("Select an option"),
  Date of Birth, Experience months, Current Salary, Expected Salary,
  Available-to-join (days), second combobox, Skills** all empty, submit
  disabled.
- Prompt gap: no variant says a *form under a Simplify-branded page title on
  a careers/applyjob URL is still the employer form — fill it*. Only the
  shadow-root probe is mentioned ("keka/Simplify-proxy pages have no such
  shadow root"), which tells the agent the probe fails, not that the page is
  the form.
- Secondary gap: after ~55 calls on ~7 non-identity fields, the "two distinct
  attempts then block" rule did not hold — the agent kept finding new
  approaches. Non-identity fields (compensation, availability, dates, gender)
  need a hard per-field budget.

## FM-2 — upload-first apply gate (freshteam)

- Runs `0088c68a` (14 calls, blocked) and `eca3fcb0` (21 calls, failed): the
  freshteam page shows **"Apply With Resume *"** (with `*` markers) as the
  first gate; both runs died at this stage without reaching the form.
- Prompt gap: no variant explicitly recognizes an "Apply With Resume" /
  upload-first gate as the first step (open → upload resume → form renders).
  The orchestrator.md's autofill section warns the employer's "Apply with
  resume" is NOT the Simplify autofill — correct — but the positive action
  ("this IS the resume-upload gate; upload immediately") is buried.

## FM-3 — form never opened (greenhouse, all three variant runs)

- Protocol run `2a8b8d72` (12 calls, failed): last obs still on the job
  description page (heading "Software Engineer I", "Apply" button visible,
  Simplify panel showing "Autofill this job application!" + resume attached).
  The form never opened; the run ended around a `fill` (console shows
  `selectText`/`fill` debugger calls) — fills were attempted against the JD
  page or the panel.
- Outcome run `649a0f01` (27 calls, blocked): same page state at the end.
- Prompt gap: the Simplify panel CTA on the *job-description* page is
  ambiguous — does clicking it open the form? The prompts order OPEN before
  AUTOFILL but don't state what to do when the Simplify panel's
  "Autofill this job application!" CTA is visible while the form is closed.

## FM-4 — correct early block (workable)

- Run `2f6c9fba` (3 calls, blocked): job URL is a dead `view/example` page
  ("This job can't be found"). Blocked after 2 orchestrator calls — the
  guard rails worked. Not a failure.

## FM-5 — good behaviors to preserve

- Optional fields (Middle Name) left empty when optional — correct.
- `<secret>` email treated as filled — correct (per tool rules).
- Phone entered with full international dial code `+919063812386` — correct.

## What this means for prompt iterations

1. Add an explicit **page-identity rule**: the page is "the form" if it shows
   form controls (textboxes/comboboxes under required labels), *regardless of
   page title or branding* (keka/Simplify proxy, Greenhouse embedded form,
   freshteam). Never loop on page recognition; if the evidence shows form
   controls, treat it as the form.
2. Add an explicit **upload-first gate** rule: "Apply With Resume" / resume
   dropzone with `*` = upload the resume now, then continue.
3. Give the Simplify panel CTA on the JD page a decision: activate it once;
   if the form is not open afterward, click the employer's Apply control.
4. Enforce a **hard per-field budget** for non-identity fields (memory once →
   AnswerWriter once → one human ask → block), so a 62-call keka run becomes
   ~a dozen calls.
5. Keep FM-4/FM-5 behaviors (early block on bad URL, optional-field
   discipline, secret handling, phone format).

Quantified target: a clean fill+review+approval-gate run should cost
**≤ 40 calls and ≤ $0.01** on a standard Greenhouse form (the 27-call outcome
run was $0.012 and never reached the form; the 62-call keka run was $0.025).

## Fixes landed (2026-08)

- `orchestrator.md` rewritten compact (39.8KB → 11.5KB): single-source rules,
  no 3–4× rule repetition, and explicit page-identity (FM-1), upload-first
  gate (FM-2), and Simplify-panel-on-JD-page (FM-3) rules plus a hard
  per-field budget of ≤4 calls. Old default preserved as
  `orchestrator-legacy.md` for A/B via the eval harness.
- `load_prompt(..., with_rules=False)` for AnswerWriter and Vision specialists:
  no longer prepend the 7KB browser-mutation rules to agents with no
  form-mutation tools (token + focus win for flash models).
- `scripts/eval_prompts.py`: same-job × variants comparison (status, calls,
  tokens, cost, duration, terminal reason); never submits real applications
  (eval human channel declines approval and raises on asks).
- Call ledger now records `terminal_reason` for every run (CLI + backend
  paths), so blocked/failed runs are analyzable without digging obs files.

Next: run the eval script across ≥2 sites (greenhouse + keka + freshteam)
with `--variants ''` and compare legacy vs compact vs the flash-style
variants.
