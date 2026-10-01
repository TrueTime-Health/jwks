# The fleet PR review — one copy (forge#307)

Every TrueTime repo's `claude-code-review.yml` is a thin caller of the composite actions in
this directory. Change review behaviour HERE, once. Do not edit a product repo's caller.

| Piece | What it does |
|---|---|
| `pr_review_gate.py` | Every decision: the verdict parser, round cap, model choice, fallback trigger, attribution stamp, the gate and its override doors, the conflict check. Stdlib only. Tested by `test_pr_review_gate.py` at the forge root. |
| `prompt.md` | The one review prompt, used by the primary and the fallback. |
| `start/` | Review job: run-start anchor, 7-round stand-down, reviewer models from Forge. |
| `review/` | Runs one model (primary or fallback) with the shared prompt. |
| `has-verdict/` | Fallback job: did the primary answer? did the fallback? Fails on a missing key. |
| `gate/` | Gate job: stamp `REVIEWED-BY`, then enforce. |
| `mergeable/` | forge#307 seam 1: red check + comment when the branch conflicts with its base. |
| `callers/` | The two files each repo copies **byte-for-byte** into `.github/workflows/`. |

## Verdicts

| Verdict | Gate | Human door |
|---|---|---|
| `REVIEW: PASS` | passes | none needed |
| `REVIEW: ISSUES` | blocks | `review:override` label, or the Forge console override |
| `REVIEW: UNVERIFIED` | blocks | `review:unverified-ok` label, applied by a human **after** the verdict; or either override door |
| no verdict | blocks | override doors |

`UNVERIFIED` is forge#307 seam 2: a claim the reviewer could not check from the checkout
(code outside the repo, a base image, an external identifier) no longer passes with a caveat.
Labels count only while present and only when the most recent application was by a human.

## Per-repo configuration

Optional `.github/pr-review.json`, **read from the PR's base commit** so a PR cannot rewrite
the instructions of the reviewer judging it:

```json
{
  "prompt_extra": "RUN THE SUITE — python -m pytest tests/ -q ...",
  "python_version": "3.12",
  "install": "pip install -q -r requirements.txt pytest",
  "allowed_tools": "Bash(python -m pytest:*),Bash(pip install:*)",
  "override_allowlist": "sumitcareteams"
}
```

Unknown keys fail the run loudly — a typo must not silently drop an instruction.

## Adopting it in a repo

1. Copy `callers/claude-code-review.yml` and `callers/pr-mergeable.yml` into
   `.github/workflows/`, unchanged.
2. Move anything repo-specific from the old workflow into `.github/pr-review.json`.
3. The adopting PR edits the reviewer workflow, so the platform skips its review by design
   and it needs a human `review:override`. That is the LAST time a reviewer change needs one:
   afterwards the caller never changes, and fixes land here.
