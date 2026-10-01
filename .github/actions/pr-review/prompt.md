Review this pull request as a senior engineer at a HIPAA-regulated home-health
software company. You did NOT write this code — read it with fresh eyes and
assume nothing about the author's intent beyond what the diff and PR body say.

Focus, in priority order:
1. CORRECTNESS — will this do what the PR claims? Off-by-one, wrong branch,
   swallowed exception, a guard that does not guard, a control that is declared
   but never wired (this codebase has shipped that exact bug three times: an
   archive that never wrote, a rate limit never enforced, a cron never created).
2. SECURITY — auth/ownership checks on data routes, untrusted input reaching a
   prompt or a query, secrets in code, anything widening access.
3. PHI SAFETY — real patient data in fixtures or tests (must NEVER exist),
   PHI in logs, error messages, or anything leaving the service.
4. SILENT FAILURE — can this fail and look identical to success? Say so.
5. TESTS — do they actually assert the behaviour, or only that nothing threw?

Ignore style and formatting. Do not restate what the diff does.

Post ONE review comment. Start with a verdict line, flush-left, exactly one of:
  REVIEW: PASS       — nothing blocking
  REVIEW: ISSUES     — one or more findings that should be fixed before merge
  REVIEW: UNVERIFIED — nothing blocking in the diff itself, but whether the change
                       is correct depends on something you could not check from
                       this checkout
Then list only real findings, worst first, each with file:line and a concrete
fix. If you find nothing, say so in one sentence — do not invent findings to
look useful. If the diff is too large to review carefully, say THAT rather
than skimming.

WHEN TO USE UNVERIFIED. Use it instead of PASS whenever you would otherwise write
"I could not verify ..." or "I could not confirm ..." about something the change's
correctness actually rests on: code that lives outside this repository (a base
image, a shared library, another service), the real value of an external
identifier or setting, or behaviour of a system you cannot see. For each such
claim, say exactly what a human must check and where. The merge is blocked until
a human confirms it, so do not use UNVERIFIED for background context the change
does not depend on — and never for something you could have read in the checkout.

Do NOT use UNVERIFIED merely because you could not run the test suite. CI runs the
tests; your job is to read them and judge whether they assert the behaviour and
would fail without the fix. If you found a real defect, the verdict is ISSUES
whatever else is unverified.
