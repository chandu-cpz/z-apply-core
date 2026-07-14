# Z-Apply Core Runtime: Technical Postmortem

## Outcome

Z-Apply Core reached its first meaningful product milestone: it filled a
multi-step Workday application and reached the approval gate, including
authentication, Simplify-assisted filling, direct resume upload, and recovery
across imperfect model turns. This was not achieved by finding one unusually
capable model. It came from giving interchangeable models a better runtime
contract.

## What originally failed

Early runs were long, expensive, and brittle. Specialists often returned
convincing prose without performing the required tool call. The orchestrator then
accepted that prose as progress, repeated already-completed operations, or lost
the browser state altogether. Field mapping repeatedly terminated without its
required recording call. Some models emitted JSON-looking tool calls in assistant
content instead of the native tool-call channel. Other endpoints timed out,
rate-limited, returned malformed messages, or disappeared during a run.

The recovery machinery also worked against the application. Its no-progress
counter accumulated across the entire run, so a long but healthy application was
guaranteed to fail eventually even after many successful browser actions. Browser
decisions were sometimes based on stale snapshots, Simplify panel text, artifact
filenames, or specialist claims rather than the employer form's current DOM.

Resume upload exposed another architectural fault. Clicking an upload control and
then supplying a file were modeled as two independent operations. In a headed
Camoufox session, the first operation opened the operating system file picker on
the user's screen. The agent could then become trapped in modal state and lose
access to ordinary snapshots.

Two opposite approaches both proved insufficient. A mostly deterministic ATS
flow could not generalize across dynamic forms, custom widgets, authentication,
and ambiguous questions. A loosely constrained multi-agent flow could reason
semantically, but it mistook narrative completion for executed state and could
not reliably survive weak tool-calling models.

## Root causes

- **Prompt/protocol mismatch:** prompts described intentions, while the runtime
  required native tool calls. Models could appear successful without satisfying
  the executable contract.
- **False success:** specialist prose and prior state were treated as evidence of
  browser mutations that had not occurred.
- **Stale evidence:** decisions were made from old ARIA snapshots, Simplify UI,
  or visual artifacts instead of fresh employer-page state.
- **Incorrect recovery accounting:** cumulative prose/no-progress failures were
  counted across successful work rather than consecutively since the last action.
- **Split upload semantics:** click-then-upload exposed a native chooser and an
  unmanageable intermediate state.
- **Model churn:** rotating models on every turn destroyed continuity, while
  never rotating made one rate limit or bad protocol response fatal.
- **Over-modeled coordination:** large typed ledgers and duplicate mapping layers
  tried to reimplement reasoning that DeepAgents already performs, adding more
  contracts for weaker models to violate.

## Breakthroughs that worked

The successful architecture keeps **semantic control agentic** and **execution
boundaries deterministic**. One persistent DeepAgents orchestrator owns the
application objective and interprets the current page. Focused specialists handle
candidate answers, authentication/Gmail verification, and genuinely visual
questions. The browser session owns validated low-level actions and returns
post-action evidence. This preserves adaptability without trusting prose as state.

The browser DOM/ARIA snapshot became the workflow source of truth. Every mutation
returns current inline evidence when possible; stale references fail recoverably;
unchanged duplicate mutations are rejected. Simplify is treated as a per-form-step
accelerator, never as proof that the employer form was filled. Authentication is a
bounded delegated flow with Gmail polling and temporary verification-tab cleanup,
then control returns to the same orchestrator and browser.

The upload operation became atomic inside Core. `browser_click_upload` resolves a
visible upload trigger to its associated `input[type=file]` and calls Playwright's
`set_input_files()` directly. It never opens a native chooser. This small typed
boundary removed an entire modal failure class without adding ATS-specific code or
changing the Playwright MCP surface.

Goal recovery was changed from cumulative to consecutive. A prose-only stop adds
controller feedback and resumes the same graph, but any native executable action
resets the budget. Repeated denied actions rotate the model. Model selection is
otherwise sticky: a healthy model retains context, while transient provider
errors, rate limits, stalls, and unusable protocol responses release the lease and
select another eligible NIM model. This balances continuity with free-tier
availability.

Typed contracts are now used where facts must be enforced—browser mutation
arguments/results, terminal status, human answers, authentication verdicts,
upload, and final approval—not as a duplicate representation of the entire conversation.
The submit executor refuses final form submission until Telegram approval has
been recorded. Fresh confirmation evidence is required before marking the
application submitted.

## Why it worked

The final design assigns each kind of uncertainty to the right layer. LLMs handle
meaning: what page is visible, which field is required, which specialist is
needed, and what action comes next. The harness handles invariants: tool-channel
execution, model retry and rotation, duplicate suppression, atomic upload,
resource cleanup, approval locking, and event streaming. Neither layer attempts
to replace the other.

That is the main design constraint for the frontend and backend: they should form
a control plane around Core, not absorb application semantics. They should start
and cancel runs, persist and stream typed events, route one human conversation per
application, publish artifacts, expose an authenticated interactive noVNC session,
and support explicit take-control/return-control. Core should remain the owner of
the live browser objective.

## Remaining gaps

The first successful fill also exposed the next safety defect: submission approval
was requested while Workday still displayed **Errors Found**, with unresolved
nationality/residency fields and questionable education data. Pre-submit readiness
therefore needs a harness-enforced fresh-evidence verifier; prompt instructions
alone are insufficient. The review-PDF publisher also calls a tool name that does
not match the available browser PDF contract and needs Camoufox verification.

Finally, the CLI currently owns process lifetime, VNC startup, Telegram, and
ephemeral run state. The backend should become the durable run supervisor and
event/human-response broker; the frontend should provide logs, questions,
approval, artifacts, and noVNC watch/take-control modes. This split should be made
without weakening Core's evidence and submission boundaries.
