# Simplify Authentication

Ensure the current Simplify session is usable, then stop. Do not navigate back
to the job page; the runtime restores it.

1. Invoke BrowserSpecialist only through
   `task(subagent_type="BrowserSpecialist")` to inspect the current Simplify
   page. BrowserSpecialist is not a tool and must never be invoked directly.
2. If dashboard or account-specific evidence is visible, return
   `authenticated: Simplify is verified authenticated.`
3. If a login form is visible, invoke BrowserSpecialist through `task` to
   submit the configured secret names `DEFAULT_USERNAME` and
   `DEFAULT_PASSWORD`, then inspect the resulting evidence.
4. If CAPTCHA, OTP, email verification, browser challenge, or manual login is
   required, call `ask_human` and return `blocked` until it is resolved.

Never request or reveal raw credentials. Never print JSON that imitates a task
call. Return only `authenticated`, `blocked`, or `not_verified` with a concise
reason.
