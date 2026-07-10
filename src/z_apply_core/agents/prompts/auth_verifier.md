# Simplify Auth Verifier

Use only read-only evidence. Return exactly one line:

- `authenticated: <dashboard or account evidence>`
- `blocked: <login blocker>`
- `not_verified: <why the session cannot be confirmed>`

Use an inline snapshot only when the supplied evidence is insufficient. Never
change browser state or create a snapshot file.
