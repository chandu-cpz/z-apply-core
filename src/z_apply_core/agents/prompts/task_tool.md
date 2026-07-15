Launch one named Z-Apply specialist and return its normal final message to the
parent agent.

Available specialist types:
{available_agents}

Use `subagent_type="AnswerWriter"` for exactly one empty required candidate
field. This is intentional even though it is a small task: AnswerWriter alone
has candidate memory, resume evidence, and Telegram access. Include only the
field's exact label/question, current value, type, units/constraints, visible
options, and validation evidence. Independent fields may be launched together,
up to eight. The parent must consume all returned `<field> = <value>` results with
browser mutations before launching any more tasks.

Use `subagent_type="AuthenticationSpecialist"` for one currently visible login,
email-verification, OTP, or identity gate. Include the current URL and exact
visible gate evidence. It may mutate only that auth flow and must return fresh
browser evidence.

Specialists do not navigate, mutate the form, handle challenges, approve, or
submit. Do not request a reporting tool or a second handoff: the task's normal
final message is its result.
