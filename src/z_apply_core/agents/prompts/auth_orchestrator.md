# Simplify Authentication

Ensure the current Simplify session is usable, then stop. Do not navigate back
to the job page; the runtime restores it.

1. Call `execute_browser_operation(operation="snapshot")` to inspect the
   current Simplify page. Read the returned snapshot; BrowserSpecialist is not
   a tool and must never be invoked directly.
2. If dashboard or account-specific evidence is visible, return
   `authenticated: Simplify is verified authenticated.`
3. If a login form is visible, call `ask_human`; login submission is not yet a
   typed executor operation.
4. If CAPTCHA, OTP, email verification, browser challenge, or manual login is
   required, call `ask_human` and return `blocked` until it is resolved.

Never request or reveal raw credentials. Never print JSON that imitates a task
call. Return only `authenticated`, `blocked`, or `not_verified` with a concise
reason.
