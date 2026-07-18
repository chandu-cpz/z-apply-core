Launch one named Z-Apply specialist and return its normal final message to the
parent agent.

Available specialist types:
{available_agents}

Use `subagent_type="AnswerWriter"` for exactly one empty required candidate
field and its exact current browser target ref. This is intentional even though
it is a small task: AnswerWriter alone
has candidate memory, resume evidence, and Telegram access. Include only the
field's exact label/question, current value, type, units/constraints, visible
options, and validation evidence. Do not include a proposed candidate answer or
biographical claim; AnswerWriter retrieves candidate evidence independently.
Launch only one AnswerWriter at a time because
any unresolved field may require the single human-question channel. The parent
must consume its returned structured field result with a browser mutation
before launching another task.

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
