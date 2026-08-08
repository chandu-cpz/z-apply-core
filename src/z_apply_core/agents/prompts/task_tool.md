Launch one named Z-Apply specialist and return its normal final message to the
parent agent.

Available specialist types:
{available_agents}

Use `subagent_type="AnswerWriter"` to resolve candidate field VALUES. Pass
ONE field per task call — the exact visible label, current ref, control type,
current value, and visible options — and dispatch exactly ONE AnswerWriter
`task` call per response, one field at a time, never several fields or
several `task` calls in one response; the runtime runs whatever `task` calls
appear in one response concurrently, which is why you must keep one `task`
call in flight at once so each field resolves before the next dispatch. Never
include file-upload controls (a `Choose File` button or file input) — they are
not candidate facts; you handle uploads yourself with `browser_click_upload`.
AnswerWriter returns evidence-backed answers for the fields it can resolve and
never touches the browser — you apply them with `browser_fill_form`.

Use `subagent_type="VisionSpecialist"` only when the current typed browser
context says `visual_only_surface_visible=true` and one specific visual question
cannot be answered from ARIA/DOM. The runtime rejects ordinary-page delegation.

Use `subagent_type="AuthenticationSpecialist"` for one currently visible login,
email-verification, OTP, or identity gate. Include the current URL and exact
visible gate evidence. It may mutate only that auth flow and must return fresh
browser evidence.

Use `subagent_type="SubmissionReviewer"` ONLY when the form is complete and you
are ready for final submission. Include your complete field-by-field
`final_review` in the description. It independently verifies the page, delivers
the review image to the human, requests the human's final approval (never
twice for one application), clicks the submit control after approval, and
returns a free-text report beginning with `SUBMITTED:` or `REVIEW_FEEDBACK:`.

AnswerWriter, VisionSpecialist, and AuthenticationSpecialist do not navigate,
mutate the form, handle challenges, approve, or submit. SubmissionReviewer is
the sole exception: it is the only specialist that may click the final submit
control, and only after the human approves. Do not request a reporting tool or
a second handoff: the task's normal final message is its result.
