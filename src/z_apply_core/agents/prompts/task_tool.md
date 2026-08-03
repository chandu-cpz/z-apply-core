Launch one named Z-Apply specialist and return its normal final message to the
parent agent.

Available specialist types:
{available_agents}

Do not call `task` with `subagent_type="AnswerWriter"`. AnswerWriter handoffs
must go through the orchestrator's typed `resolve_candidate_field` tool: the
runtime rewrites each call into an AnswerWriter `task` and binds it to live
browser evidence. Emitting multiple `resolve_candidate_field` calls in one
response is encouraged; the runtime runs them concurrently. Browser mutations
remain deliberate single steps: act, read the result, then decide.

Use `subagent_type="VisionSpecialist"` only when the current typed browser
context says `visual_only_surface_visible=true` and one specific visual question
cannot be answered from ARIA/DOM. The runtime rejects ordinary-page delegation.

Use `subagent_type="AuthenticationSpecialist"` for one currently visible login,
email-verification, OTP, or identity gate. Include the current URL and exact
visible gate evidence. It may mutate only that auth flow and must return fresh
browser evidence.

Specialists do not navigate, mutate the form, handle challenges, approve, or
submit. Do not request a reporting tool or a second handoff: the task's normal
final message is its result.
