Launch one named Z-Apply specialist and return its normal final message to the
parent agent.

Available specialist types:
{available_agents}

Do not use `task` for AnswerWriter. Candidate fields must go through the
orchestrator's typed `resolve_candidate_field` tool so the runtime can bind the
request to live browser evidence.

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
