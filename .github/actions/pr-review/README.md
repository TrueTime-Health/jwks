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

## Changing the reviewer models

The models are chosen in the **production Forge console** (PR Review → reviewer models),
not here: `start/` asks Forge on every run. It falls back to `DEFAULT_PRIMARY` /
`DEFAULT_FALLBACK` in `pr_review_gate.py` in three cases, not only an outage:

- the repo is missing the `FORGE_URL` or `FORGE_API_KEY` secret;
- the request to Forge fails or takes longer than 10 seconds;
- Forge answers without both a primary and a fallback.

The defaults are deliberately the same pair as the stored defaults, so the model names alone
cannot tell you which path ran. The `start` step's log line can:
`Reviewer models — primary=… fallback=… (source: forge | workflow defaults)`.

**The reviewer must support the model.** `review/` pins `claude-code-action`, which installs a
fixed Claude Code version, and a model newer than that version fails instantly with
`API Error: 400 Claude Code <version> does not support this model`. Opus 5.5 needs Claude Code
2.1.280 or newer.

The **fallback must be a model already seen reviewing** (`reviewer_models.py`): a bad
primary is caught by the fallback, but a bad fallback means no review at all. "Seen" is
recorded from a real `REVIEWED-BY` stamp, never typed. So a new model becomes the fallback
in four steps:

1. In the console, add the model and set it as **primary**, keeping a verified fallback.
2. Let one PR be reviewed, then read the gate's `REVIEWED-BY` stamp. It names the model that
   actually answered. If it reads `<old fallback> (fallback — <new model> returned no
   verdict)`, the new model failed; the review job log says why, and nothing was verified.
   **No stamp at all** means the same: neither model answered. Use a normal PR for the
   trial, not a Dependabot patch/minor bump, which goes to the cheap pair instead.
3. While that PR is still open, load the **production** PR Review console; a staging render
   records nothing. The new model then appears in the Fallback dropdown, which lists only
   verified models. That is how you know step 3 worked.
4. Set the primary back and choose the new model as the **fallback**.

Dependabot patch/minor bumps use their own cheaper pair (`_DEPENDABOT_CHEAP` in
`reviewer_models.py`), which the console pair does not change.

## The third lane: a second subscription (forge#341)

The primary and the fallback are two **models on one credential**, the org secret
`CLAUDE_CODE_OAUTH_TOKEN`. That covers a model outage. It does not cover the subscription's
budget running out or its token expiring — then both lanes fall silent together and every PR
in the fleet blocks with `BOTH REVIEWERS SILENT` (2026-10-14).

The caller's third job, `fallback2_review`, runs the same `review/` action with the same
prompt on **`CLAUDE_CODE_OAUTH_TOKEN_2`**, a token minted from a *different* Claude
subscription. It fires only when `has-verdict` saw the fallback NOT post — never as a third
opinion — and its model is `DEFAULT_TERTIARY` in `pr_review_gate.py` (`claude-opus-5-5`),
fixed in the workflow rather than chosen in the Forge console: the lane exists for the moment
the usual machinery is down, so it depends on no remote lookup. Its `REVIEWED-BY` stamp reads
`<model> (second fallback, on the second subscription — <primary> and <fallback> returned no
verdict)`. When it is silent too, the gate says `ALL REVIEWERS SILENT` and names both secrets.

Minting the second token (a human, on a machine signed in to the *other* subscription):

```bash
claude login
claude setup-token
```

Add the printed token as an **org** Actions secret named `CLAUDE_CODE_OAUTH_TOKEN_2` (all
repositories), and to each repository's **Dependabot** secret scope, exactly as the first
token is. A repo without it fails `fallback2_review` loudly with `NO REVIEWER CREDENTIALS —
CLAUDE_CODE_OAUTH_TOKEN_2 is empty` whenever the first two lanes are silent; that is the
signal to add it, not a reason to mark the step continue-on-error. A token from the *same*
subscription as the first adds nothing: it shares the budget that just ran out.

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
   and it needs a human `review:override` (and, in repos with the security-files gate, a
   Forge-console approval of that exact commit).

## Rolling out a fix

The org requires every action pinned to a full commit SHA, first-party included, so callers
pin this directory at one forge commit. A fix merges here once; each repo then takes it by a
one-line SHA bump of its caller — the same SHA everywhere, which is what keeps the fleet from
drifting again. Those bump PRs edit the reviewer workflow, so they still need a human override.

## Rollout status

All 18 repos adopted on 2026-10-01 (forge#307), at forge `3897881`. Moved to `56d590a`
(claude-code-action v1.0.248 / Claude Code 2.1.296, forge#333) with checkout v7.0.1 in
forge#318. The third lane (forge#341) ships in the next SHA bump: every caller takes the new
`fallback2_review` job, so each bump PR edits the reviewer workflow and needs a human
`review:override` (and the Forge console approval where `security-files` runs). Add the
`CLAUDE_CODE_OAUTH_TOKEN_2` secret **before** bumping, or the new job fails on its first
silent-lanes run.

| Repo | Adopted in | How |
|---|---|---|
| forge | forge#310 | pinned |
| DTC---PAC | DTC---PAC#154 | pinned |
| Founders_workshop | Founders_workshop#156 | pinned |
| Newsletter | Newsletter#110 | pinned |
| TrueTime-----content | TrueTime-----content#103 | pinned |
| Truetime---voice | Truetime---voice#212 | pinned |
| truetime---QA | truetime---QA#168 | pinned |
| truetime---adr | truetime---adr#138 | pinned |
| truetime---api | truetime---api#426 | pinned |
| truetime---survey | truetime---survey#186 | pinned |
| truetime---tpe | truetime---tpe#22 | pinned |
| truetime---verify | truetime---verify#90 | pinned |
| truetime--pcr | truetime--pcr#127 | pinned |
| truetime-health-ap | truetime-health-ap#61 | pinned |
| truetime-wisdom-base | truetime-wisdom-base#78 | pinned |
| visit_planner | visit_planner#549 | pinned |
| jwks | jwks#24 + jwks#23 | vendored |
| emr-integration | emr-integration#45 + emr-integration#44 | vendored |

**Vendored** repos (jwks is public, emr-integration internal) cannot resolve forge's private
actions. They carry a byte-for-byte copy at `.github/actions/pr-review/` with `VENDORED_FROM`
and `MANIFEST.sha256`, loaded from the PR's base commit. On every SHA bump, re-vendor them too.
