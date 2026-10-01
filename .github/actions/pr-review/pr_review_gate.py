"""The fleet's PR review gate — ONE copy, called by every repo (forge#307).

WHY THIS FILE EXISTS
--------------------
`claude-code-review.yml` had diverged into eighteen per-repo copies, eleven of them
distinct, and the drift ran in every direction at once: forge had the `primary_answered`
attribution fix the others lacked; seventeen repos had a credentials check forge lacked;
two repos still read the override timeline without `--paginate` (the forge#151 bug), and
the verdict anchor came in three strengths. A fix landed once re-diverged the moment the
next one landed elsewhere.

Every decision the workflow makes now lives here, and each repo's workflow is a thin caller
that runs these steps through the composite actions beside this file. A repo keeps its own
workflow only for what GitHub needs at the job level (triggers, `needs`, `outputs`, the
required-check names `review` and `gate`).

WHY PYTHON, NOT THE INLINE BASH IT REPLACES
-------------------------------------------
The bash could only be tested by slicing it out of YAML and emulating jq in a shim. Here the
GitHub API is ONE function (`Api.get` / `Api.post`), so the tests replace that and run the
real decision code — nothing about a defect gets emulated away.

Stdlib only: it runs on a bare runner with no install step.

THE VERDICT CONTRACT (forge#307 seam 2)
---------------------------------------
    REVIEW: PASS        nothing blocking
    REVIEW: ISSUES      findings to fix before merge
    REVIEW: UNVERIFIED  nothing blocking in the diff, but its correctness rests on a claim
                        the reviewer could not check from the checkout

UNVERIFIED BLOCKS until a human acknowledges it. The CEO's decision (2026-09-30) on
visit_planner#524: the reviewer said it could not see `truetime_base` (Docker base image
only), passed anyway, the author answered from a stale copy, and a change that removed
audit attribution reached production. Naming the gap is correct behaviour; it is no longer
enough to pass.

Precedence is ISSUES > UNVERIFIED > PASS: when lines disagree, the only safe reading of
"maybe there is a problem" is that there is.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REVIEWER_LOGIN = "claude[bot]"
OVERRIDE_LABEL = "review:override"
# Distinct from the override on purpose. An override says "merge despite the review"; this
# says "I checked the thing the reviewer could not". It is honoured ONLY against an
# UNVERIFIED verdict — it can never clear ISSUES — and only when applied AFTER that verdict,
# so an acknowledgement of last week's unverifiable claim cannot wave through today's.
ACK_LABEL = "review:unverified-ok"
REVIEWER_WORKFLOW = ".github/workflows/claude-code-review.yml"
REPO_CONFIG = ".github/pr-review.json"
CONFLICT_MARKER = "<!-- forge-pr-review:conflict -->"
ESCALATION_MARKER = "<!-- forge-pr-review:escalated -->"

DEFAULT_PRIMARY = "claude-fable-5"
DEFAULT_FALLBACK = "claude-opus-5"
ESCALATE_AFTER = 7

# ── The verdict parser ───────────────────────────────────────────────────────────
#
# ANCHORED AT COLUMN 0 (forge#143, and the reason it is not `^\s*`). A verdict is always
# emitted flush-left; an indented or blockquoted one (`> REVIEW: PASS`, or the format shown
# inside a code block) is someone QUOTING a verdict — innocently or by injection — not
# casting one. Heading and emphasis markers stay allowed: the reviewer really writes
# `### REVIEW: ISSUES` and `**REVIEW: PASS**`.
#
# `**REVIEW:** ISSUES` (emphasis closing on the colon) is the same flush-left verdict — the
# console accepted it before the two parsers were merged (test_override_visibility).
#
# `\b` after the word: `REVIEW: PASSED` is prose, not the contract.
_VERDICT_LINE = re.compile(
    r"^(?:[#*_]{1,4}[ \t]*)?REVIEW:(?:\*\*|__)?[ \t]*(PASS|ISSUES|UNVERIFIED)\b")
_FENCE = re.compile(r"^[ \t]*(?:```|~~~)")


def verdict_of(body: str) -> str:
    """ISSUES / UNVERIFIED / PASS, or "" when the body casts no verdict.

    Fences are handled ASYMMETRICALLY, and that is the point. A blocking verdict counts
    wherever it sits flush-left, fenced or not — an unbalanced fence must never be able to
    hide a real ISSUES. A PASS counts only OUTSIDE a fence — a review showing the format in
    a code block is discussing a verdict, not granting one. Each direction is the safe one.
    """
    found = set()
    in_fence = False
    for line in (body or "").splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        m = _VERDICT_LINE.match(line)
        if not m:
            continue
        v = m.group(1)
        if v == "PASS" and in_fence:
            continue
        found.add(v)
    for v in ("ISSUES", "UNVERIFIED", "PASS"):
        if v in found:
            return v
    return ""


# ── GitHub access ─────────────────────────────────────────────────────────────────

class Api:
    """The only thing that talks to GitHub. Tests replace it; nothing else needs to."""

    def __init__(self, token: str, base: str = "https://api.github.com"):
        self.token = token
        self.base = base.rstrip("/")

    def _req(self, method, url, data=None, accept="application/vnd.github+json"):
        req = urllib.request.Request(url, method=method, data=data)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", accept)
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
            return (json.loads(raw) if raw else None), r.headers.get("Link", "")

    def get(self, path: str, paginate: bool = False):
        """GET one resource, or EVERY page of a list.

        Pagination is not optional for timelines and comments. The timeline pages at 30
        events; an unpaginated read saw only page one, so on a long-lived PR the override
        label — always the most recent event — fell off and a real human override was
        silently ignored (forge#151). Two fleet copies still had that bug on 2026-09-30.
        """
        sep = "&" if "?" in path else "?"
        url = f"{self.base}/{path}{sep}per_page=100" if paginate else f"{self.base}/{path}"
        if not paginate:
            return self._req("GET", url)[0]
        out = []
        while url:
            page, link = self._req("GET", url)
            out.extend(page or [])
            m = re.search(r'<([^>]+)>;\s*rel="next"', link or "")
            url = m.group(1) if m else ""
        return out

    def post(self, path: str, body: dict, method: str = "POST"):
        return self._req(method, f"{self.base}/{path}", json.dumps(body).encode())[0]


def _is_human(event: dict) -> bool:
    """A POSITIVE test. `!= "Bot"` would call a missing or unexpected actor type human."""
    return ((event.get("actor") or {}).get("type") or "") == "User"


def reviewer_comments(api, repo, pr, since: str):
    """claude[bot] comments at or after `since`, oldest first.

    `since` MUST be non-empty; callers decide what an empty anchor means. Every string is
    >= "", so an empty anchor would match every verdict ever posted on the PR and a PASS
    left on an earlier commit would approve whatever was pushed after it.
    """
    assert since, "an empty anchor matches every verdict ever posted — refuse it upstream"
    return [c for c in api.get(f"repos/{repo}/issues/{pr}/comments", paginate=True)
            if (c.get("user") or {}).get("login") == REVIEWER_LOGIN
            and (c.get("created_at") or "") >= since]


def latest_review(api, repo, pr, since):
    mine = reviewer_comments(api, repo, pr, since)
    return mine[-1] if mine else None


# The header claude-code-action itself writes as line one of every verdict comment:
#   **Claude finished @user's task in 1m 2s** —— [View job](https://github.com/O/R/actions/runs/N)
# (src/github/operations/comment-logic.ts). Only the FIRST line is read — anything below it is
# model output, and a prompt-injected model could print a link of its choosing there.
_JOB_LINK = re.compile(r"^\*\*Claude (?:finished|encountered)[^\n]*? —— \[View job\]"
                       r"\(https://github\.com/([^/\s)]+/[^/\s)]+)/actions/runs/(\d+)\)")


def review_is_for_head(api, repo, review, head_sha) -> str:
    """"" when the verdict comment was written by a run on `head_sha`, else why not.

    WHY THIS EXISTS (forge#309 review, round 1). On a label event there is no run timestamp,
    and the first version anchored to the head commit's COMMITTER DATE — which the author
    writes (GIT_COMMITTER_DATE). Backdate a commit, add any label, and a PASS left on an
    EARLIER commit is newer than the "commit", so it approved code no model had read. Worse,
    make that commit one that edits the reviewer workflow and the platform guarantees no new
    verdict ever appears to compete with the stale one.

    So a verdict is tied to code by what GitHub records, not by a clock: the run that wrote
    it is named in the header the action writes, and the run's head_sha is server-assigned.
    Anything unreadable, missing or mismatched is a reason — and every reason blocks.
    """
    if not head_sha:
        return "no head SHA in the event"
    first = ((review or {}).get("body") or "").split("\n", 1)[0]
    m = _JOB_LINK.match(first)
    if not m:
        return ("the review comment carries no run link in claude-code-action's header, so "
                "it cannot be tied to a commit (did the action's header format change?)")
    if m.group(1).lower() != repo.lower():
        return f"the review comment links a run in another repository ({m.group(1)})"
    try:
        run_sha = (api.get(f"repos/{repo}/actions/runs/{m.group(2)}") or {}).get("head_sha", "")
    except Exception as e:  # noqa: BLE001 — unknown is a reason, and reasons block
        return f"could not read run {m.group(2)}: {e}"
    if run_sha != head_sha:
        return (f"the latest verdict was written for {run_sha[:12] or 'an unknown commit'}, "
                f"not for the head {head_sha[:12]}")
    return ""


def human_label(api, repo, pr, label, allowlist=()):
    """The login of the human whose application of `label` currently stands, else "".

    Three conditions, all required:
      * the label is ON the PR now — so removing it withdraws it;
      * the MOST RECENT application was by a non-Bot actor — a bot re-applying after a
        human removed it is a bot's decision, and the agent must never be able to wave its
        own work through by labelling its own PR;
      * if the repo sets an allowlist, the human is on it.

    Returns (login, applied_at) so a caller can scope the decision in time.
    """
    labels = api.get(f"repos/{repo}/issues/{pr}/labels", paginate=True)
    if label not in {(l or {}).get("name") for l in labels}:
        return "", ""
    applied = [e for e in api.get(f"repos/{repo}/issues/{pr}/timeline", paginate=True)
               if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == label]
    if not applied:
        return "", ""
    last = applied[-1]
    login = (last.get("actor") or {}).get("login") or ""
    if not _is_human(last):
        print(f"::warning::The standing {label} label was last applied by a bot ({login}) — ignored.")
        return "", ""
    if allowlist and login not in allowlist:
        print(f"::warning::{label} was applied by {login}, who is not on this repo's allowlist "
              f"({' '.join(allowlist)}) — ignored.")
        return "", ""
    return login, last.get("created_at") or ""


def forge_override(forge_url, forge_key, repo, pr, opener=urllib.request.urlopen):
    """Door two: an override recorded in the Forge console, IAP-verified. Best-effort.

    Forge down degrades to "no override found" — it must never stop the verdict from
    being evaluated. Fail-closed stays with the verdict.
    """
    if not (forge_url and forge_key):
        return ""
    try:
        q = urllib.parse.urlencode({"repo": repo, "pr": str(pr)})
        req = urllib.request.Request(f"{forge_url.rstrip('/')}/api/v1/review-override?{q}")
        req.add_header("X-Forge-API-Key", forge_key)
        with opener(req, timeout=10) as r:
            d = (json.loads(r.read().decode() or "{}") or {}).get("override") or {}
        return d.get("by", "") or ""
    except Exception:  # noqa: BLE001 — best-effort by design, see docstring
        return ""


# ── The gate ────────────────────────────────────────────────────────────────────

class Ctx:
    """Everything the gate reads from the event, as plain values."""

    def __init__(self, env=None):
        e = env if env is not None else os.environ
        self.repo = e.get("PR_REPO", "")
        self.pr = e.get("PR_NUMBER", "")
        self.run_started = e.get("RUN_STARTED", "")
        self.action = e.get("EVENT_ACTION", "")
        self.label = e.get("LABEL_NAME", "")
        self.head_sha = e.get("HEAD_SHA", "")
        self.forge_url = e.get("FORGE_URL", "")
        self.forge_key = e.get("FORGE_API_KEY", "")
        self.primary = e.get("PRIMARY", "") or "the primary model"
        self.fallback = e.get("FALLBACK", "")
        self.primary_answered = e.get("PRIMARY_ANSWERED", "")
        self.fallback_posted = e.get("FALLBACK_POSTED", "")
        self.allowlist = tuple((e.get("OVERRIDE_ALLOWLIST", "") or "").split())


def enforce(ctx: Ctx, api, forge_opener=urllib.request.urlopen) -> int:
    """0 = merge unblocked, 1 = blocked. Every uncertain path returns 1."""
    # DOOR ONE — the human review:override label. Read BEFORE the run-timestamp guard: a
    # PR that edits the reviewer caller is skipped by the platform and can never earn a
    # verdict, so if the guard came first the human label could never open it.
    who, _ = human_label(api, ctx.repo, ctx.pr, OVERRIDE_LABEL, ctx.allowlist)
    if who:
        print(f"::warning::Review gate OVERRIDDEN by @{who} via the {OVERRIDE_LABEL} label — "
              "merged on a human decision, not a review.")
        return 0
    # DOOR TWO — the Forge console (carries an IAP-verified identity a stolen GitHub token
    # cannot forge; the 2026-08-05 lesson).
    fovr = forge_override(ctx.forge_url, ctx.forge_key, ctx.repo, ctx.pr, forge_opener)
    if fovr:
        print(f"::warning::Review gate OVERRIDDEN by {fovr} via the Forge console "
              "(IAP-verified) — merged on a human decision, not a review.")
        return 0

    # A LABEL EVENT CARRIES NO NEW CODE. The review job is skipped for it, so there is no
    # run timestamp — but the verdict already recorded for THIS COMMIT is still the right
    # answer. Which comment is "for this commit" is decided by review_is_for_head below
    # (server-recorded run SHA), never by a timestamp the author can write. If the latest
    # verdict is for an older commit — or the code-triggered review is still running — this
    # blocks, and that run's own gate overwrites it when it finishes.
    label_lane = not ctx.run_started and ctx.action in ("labeled", "unlabeled")
    if not ctx.run_started and not label_lane:
        print("::error::No run-start timestamp — BLOCKED. The review job was skipped or died "
              "before recording one, so this check cannot tell a verdict for THIS code from "
              "one left on an earlier commit. If this PR is a draft, mark it ready for "
              "review; otherwise re-run the review job.")
        return 1

    if label_lane:
        mine = [c for c in api.get(f"repos/{ctx.repo}/issues/{ctx.pr}/comments", paginate=True)
                if (c.get("user") or {}).get("login") == REVIEWER_LOGIN]
        review = mine[-1] if mine else None
        if review:
            print(f"Label event '{ctx.label}' carries no new code — re-asserting the latest "
                  f"verdict, if it was written for {ctx.head_sha}.")
    else:
        review = latest_review(api, ctx.repo, ctx.pr, ctx.run_started)
    body = (review or {}).get("body") or ""
    if not body:
        return _explain_silence(ctx, api)
    # Checked on BOTH lanes. On the code lane run_started already scopes it; this is the
    # second, independent reason a stale verdict cannot pass.
    why = review_is_for_head(api, ctx.repo, review, ctx.head_sha)
    if why:
        print(f"::error::The review cannot be tied to this commit — BLOCKED: {why}. A verdict "
              "on other code is not a verdict on this code. Push, or re-run the review job.")
        return 1

    verdict = verdict_of(body)
    # CAPACITY vs CODE: an errored run with no verdict is an outage, not a finding.
    if not verdict and re.search(r"encountered an error|execution failed", body, re.I):
        print("::error::NO REVIEWER AVAILABLE — both models failed to answer. This is a "
              "CAPACITY problem, not a finding: your code has not been judged. Check the "
              "Claude subscription budget, then re-run this job. Merge stays blocked because "
              "an unreviewed PR is not an approved one.")
        print("\n".join(body.splitlines()[:20]))
        return 1
    if verdict == "ISSUES":
        print("::error::REVIEW: ISSUES — merge blocked. Fix the findings in the review "
              "comment and push; this re-runs automatically on every commit.")
        print("\n".join(body.splitlines()[:60]))
        return 1
    if verdict == "UNVERIFIED":
        who, at = human_label(api, ctx.repo, ctx.pr, ACK_LABEL, ctx.allowlist)
        verdict_at = review.get("created_at") or ""
        if who and at and at >= verdict_at:
            print(f"::warning::REVIEW: UNVERIFIED acknowledged by @{who} via {ACK_LABEL} at "
                  f"{at} — a human confirmed what the reviewer could not check.")
            return 0
        if who:
            print(f"::warning::{ACK_LABEL} predates this verdict ({at} < {verdict_at}) — it "
                  "acknowledged an EARLIER unverifiable claim, not this one.")
        print(f"::error::REVIEW: UNVERIFIED — merge blocked until a human checks what the "
              f"reviewer could not. Read the claims listed in the review, confirm each one "
              f"against the real source (not a local copy), then apply the {ACK_LABEL} label. "
              f"An agent cannot apply it for you.")
        print("\n".join(body.splitlines()[:60]))
        return 1
    if verdict == "PASS":
        print("✅ REVIEW: PASS — merge unblocked.")
        return 0
    print("::error::No verdict line found in the review comment — treating as BLOCKED. A "
          "verdict must start its own line, flush-left, as 'REVIEW: PASS', 'REVIEW: ISSUES' "
          "or 'REVIEW: UNVERIFIED' (markdown heading or bold markers are fine).")
    print("\n".join(body.splitlines()[:40]))
    return 1


def _explain_silence(ctx, api) -> int:
    """No reviewer comment since the anchor. Always blocks; the job is naming WHY.

    Same red X, causes that call for opposite responses — so ask what is actually true.
    """
    try:
        files = api.get(f"repos/{ctx.repo}/pulls/{ctx.pr}/files", paginate=True)
        edits = any(f.get("filename") == REVIEWER_WORKFLOW for f in files)
    except Exception:  # noqa: BLE001 — unknown falls through to the generic message
        edits = False
    if edits:
        print(f"::error::UNREVIEWABLE BY DESIGN — this PR edits {REVIEWER_WORKFLOW}, so the "
              "branch's copy no longer matches main and claude-code-action refuses to start. "
              "No model has read this diff, and rebasing cannot fix it. This is NOT a "
              f"capacity problem. A human can apply the {OVERRIDE_LABEL} label, or override "
              "in the Forge console; that is a genuinely unreviewed merge, not a rubber stamp.")
        return 1
    if ctx.primary_answered == "false" and ctx.fallback_posted == "false":
        print(f"::error::BOTH REVIEWERS SILENT — {ctx.primary} returned no verdict and the "
              "fallback did not post one either. This is an INFRASTRUCTURE problem, not a "
              "finding: your code has NOT been judged. Check the Claude subscription budget "
              "and that CLAUDE_CODE_OAUTH_TOKEN is in this run's secret scope, then re-run.")
        return 1
    print("::error::The reviewer posted no comment — BLOCKED. A review that did not run is "
          "not an approval.")
    return 1


# ── Did a model answer? (fallback trigger, and the fallback's own report) ────────────

def has_verdict(api, repo, pr, since, on_missing="fail") -> str:
    """"true" / "false" — or, with on_missing="unknown", "unknown" for no anchor.

    Any of the three verdicts counts. This decides whether the FALLBACK runs, and the
    fallback must fire only on ABSENCE of a verdict, never on an unwelcome one — otherwise
    the gate reads whichever model happened to be kinder. UNVERIFIED is an answer.
    """
    if not since:
        if on_missing == "unknown":
            return "unknown"
        raise SystemExit("::error::No run-start timestamp — cannot tell whether the primary "
                         "model answered for THIS code.")
    r = latest_review(api, repo, pr, since)
    return "true" if r and verdict_of(r.get("body") or "") else "false"


def stamp(ctx: Ctx, api) -> None:
    """Post REVIEWED-BY only when a model actually produced a verdict for this run.

    Driven by what is ON the PR, never by what we assume happened: on a PR the platform
    skips, no model answered, and a stamp would invent a reviewer. Exact-match on
    primary_answered — an empty value (the fallback job died) must not default to either.
    """
    if not ctx.run_started:
        return
    r = latest_review(api, ctx.repo, ctx.pr, ctx.run_started)
    if not r or not verdict_of(r.get("body") or ""):
        print("::warning::No verdict from any model — nothing to attribute, so no stamp.")
        return
    if ctx.primary_answered == "true":
        note, model = "", ctx.primary
    elif ctx.primary_answered == "false":
        note, model = f" (fallback — {ctx.primary} returned no verdict)", ctx.fallback
    else:
        print("::warning::primary_answered is missing — cannot attribute, so no stamp.")
        return
    try:
        api.post(f"repos/{ctx.repo}/issues/{ctx.pr}/comments",
                 {"body": f"🤖 REVIEWED-BY: `{model}`{note}"})
    except Exception:  # noqa: BLE001
        print("::warning::could not stamp the reviewing model onto the PR")


# ── Before the review: round cap and model choice ─────────────────────────────────

def count_bounces(api, repo, pr, comments=None) -> int:
    """Blocking reviews so far. Only ISSUES counts: UNVERIFIED is not a disagreement."""
    if comments is None:
        comments = api.get(f"repos/{repo}/issues/{pr}/comments", paginate=True)
    return sum(1 for c in comments
               if (c.get("user") or {}).get("login") == REVIEWER_LOGIN
               and verdict_of(c.get("body") or "") == "ISSUES")


def escalate(api, repo, pr, bounces, title, url, forge_url, forge_key,
             opener=urllib.request.urlopen, comments=None) -> bool:
    """Stand down after the cap — ONCE. Returns whether it posted.

    Posted by the Actions bot with NO verdict line, so the gate keeps reading the last
    ISSUES — an escalation must never read as a pass.

    Idempotent (forge#309 review): the bounce count never falls, so without the marker every
    later push, and the human's own override click, re-posted the stand-down comment and
    re-sent Sumit the email.
    """
    if comments is None:
        comments = api.get(f"repos/{repo}/issues/{pr}/comments", paginate=True)
    if any(ESCALATION_MARKER in (c.get("body") or "") for c in comments):
        print("Already escalated on this PR — not posting or emailing again.")
        return False
    posted = api.post(f"repos/{repo}/issues/{pr}/comments", {"body": (
        f"{ESCALATION_MARKER}\n🛑 **Review loop stopped after {bounces} rounds.**\n\n"
        f"The reviewer has blocked this PR {bounces} times and is standing down — after this "
        "many rounds, either the finding is wrong or the fix keeps missing it, and another "
        "round cannot settle that.\n\n"
        "**The merge is still blocked.** This ends the arguing, not the protection.\n\n"
        "Sumit has been emailed. In Forge → **PR Review** he can read the findings, ask "
        "OpenAI for an independent second opinion, or override with a reason.")})
    if not (forge_url and forge_key):
        return True
    try:
        req = urllib.request.Request(
            f"{forge_url.rstrip('/')}/api/v1/review-escalation", method="POST",
            data=json.dumps({"repo": repo, "number": int(pr), "bounces": bounces,
                             "title": (title or "")[:200], "url": url}).encode())
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Forge-API-Key", forge_key)
        opener(req, timeout=20).close()
    except Exception:  # noqa: BLE001
        # The comment already says "Sumit has been emailed", and the marker means no later
        # run will retry — so correct the comment rather than leave a success message over
        # a lost email (forge#309 round 2).
        print("::warning::escalation email call failed — the PR is still blocked")
        try:
            api.post(f"repos/{repo}/issues/comments/{(posted or {}).get('id')}", {"body": (
                f"{ESCALATION_MARKER}\n🛑 **Review loop stopped after {bounces} rounds.**\n\n"
                "**The merge is still blocked.**\n\n⚠️ The escalation email to Sumit "
                "could NOT be sent (Forge did not answer). Reach him another way; in Forge → "
                "**PR Review** he can read the findings, get a second opinion, or override.")},
                method="PATCH")
        except Exception:  # noqa: BLE001
            print("::warning::could not correct the escalation comment either")
    return True


def resolve_models(forge_url, forge_key, author, title, repo, pr,
                   opener=urllib.request.urlopen):
    """(primary, fallback, source). Asked of Forge; NEVER blocking.

    This decides WHICH model reviews, never WHETHER review happens. Forge unreachable,
    slow or returning nonsense all fall back to the defaults. Author and title are
    attacker-controllable, so they only ever travel as urlencoded query VALUES.
    """
    if forge_url and forge_key:
        try:
            q = urllib.parse.urlencode({"pr_author": author or "", "pr_title": title or "",
                                        "pr_repo": repo or "", "pr_number": str(pr or "")})
            req = urllib.request.Request(f"{forge_url.rstrip('/')}/api/v1/reviewer-models?{q}")
            req.add_header("X-Forge-API-Key", forge_key)
            with opener(req, timeout=10) as r:
                d = json.loads(r.read().decode() or "{}") or {}
            p, f = d.get("primary") or "", d.get("fallback") or ""
            if isinstance(p, str) and isinstance(f, str) and p and f:
                return p, f, "forge"
        except Exception:  # noqa: BLE001
            pass
    return DEFAULT_PRIMARY, DEFAULT_FALLBACK, "workflow defaults"


# ── The prompt, and the repo's own additions to it ────────────────────────────────

def repo_config(api, repo, base_sha) -> dict:
    """`.github/pr-review.json` as it stands on the BASE commit — never the PR's head.

    Read from the base on purpose. truetime---api and truetime-health-ap loaded their
    prompt from the checkout, which is the PR's own head: a PR could rewrite the
    instructions of the reviewer judging it. Only a merged change may alter the review.
    """
    if not base_sha:
        return {}
    try:
        meta = api.get(f"repos/{repo}/contents/{REPO_CONFIG}?ref={base_sha}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise
    import base64
    cfg = json.loads(base64.b64decode(meta.get("content") or "").decode("utf-8") or "{}")
    if not isinstance(cfg, dict):
        raise SystemExit(f"::error::{REPO_CONFIG} must be a JSON object")
    unknown = set(cfg) - {"prompt_extra", "python_version", "install", "allowed_tools",
                          "override_allowlist"}
    if unknown:
        # Loud, because a typo'd key would otherwise silently drop the repo's instruction.
        raise SystemExit(f"::error::{REPO_CONFIG} has unknown keys: {sorted(unknown)}")
    return cfg


def build_prompt(base_prompt: str, cfg: dict) -> str:
    if not base_prompt.strip():
        raise SystemExit("::error::the shared review prompt is empty — refusing to review "
                         "with no instructions (a review that asks for nothing still posts "
                         "and still looks like a review).")
    extra = (cfg.get("prompt_extra") or "").strip()
    return base_prompt.rstrip() + ("\n\nREPOSITORY-SPECIFIC:\n" + extra if extra else "") + "\n"


def claude_args(model: str, cfg: dict) -> str:
    tools = (cfg.get("allowed_tools") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", model or ""):
        raise SystemExit(f"::error::refusing a malformed model id {model!r}")
    if not tools:
        return f"--model {model}"
    # An allowlist, not a denylist of quote characters: backticks, $() and ; must not reach
    # an argument string the action splits.
    if not re.fullmatch(r"[A-Za-z0-9_:*.,()/ \-]+", tools):
        raise SystemExit("::error::allowed_tools may only contain tool names and patterns "
                         "like Bash(python -m pytest:*)")
    base = "Glob,Grep,LS,Read,mcp__github_comment__update_claude_comment"
    return f'--model {model} --allowedTools "{base},{tools}"'


# ── forge#307 seam 1: a conflicting PR must not look merely under-checked ──────────

def mergeable(api, repo, pr, tries=10, wait=6.0, sleep=time.sleep) -> int:
    """Red, with a comment saying why, when the branch conflicts with its base.

    GitHub cannot build a merge commit for a conflicting branch, so NO `pull_request`
    workflow runs — no review, no payload check, no deploy. This runs on
    `pull_request_target`, which fires regardless. It never checks out the PR's code.
    """
    state = None
    for i in range(tries):
        state = api.get(f"repos/{repo}/pulls/{pr}").get("mergeable")
        if state is not None:
            break
        if i < tries - 1:
            sleep(wait)
    if state is None:
        print("::warning::GitHub has not computed mergeability yet — cannot tell. The review "
              "gate still blocks any PR it did not review.")
        return 0
    comments = api.get(f"repos/{repo}/issues/{pr}/comments", paginate=True)
    posted = [c for c in comments if CONFLICT_MARKER in (c.get("body") or "")]
    if state:
        if posted:
            api.post(f"repos/{repo}/issues/comments/{posted[-1]['id']}",
                     {"body": CONFLICT_MARKER + "\n✅ The conflict is resolved; the review "
                              "runs on the new head as normal."}, method="PATCH")
        print("Branch merges cleanly with its base.")
        return 0
    msg = (CONFLICT_MARKER + "\n⚠️ **NOT REVIEWED — this branch conflicts with its base.**\n\n"
           "GitHub cannot build a merge commit for a conflicting branch, so the AI review, "
           "the payload check and the synth deploy have **not run at all**. The checks you "
           "can see are not the full set.\n\nResolve the conflict (rebase or merge the base "
           "branch) and push; every check then runs on the new head. Do not resolve it in the "
           "GitHub UI and merge in the same sitting — the review has to see the result first.")
    if posted:
        api.post(f"repos/{repo}/issues/comments/{posted[-1]['id']}", {"body": msg},
                 method="PATCH")
    else:
        api.post(f"repos/{repo}/issues/{pr}/comments", {"body": msg})
    print("::error::This branch conflicts with its base — the review and the other "
          "pull_request checks cannot run until it is resolved.")
    return 1


# ── CLI: one subcommand per composite step ────────────────────────────────────────

def _out(**kv):
    """Write step outputs. Multi-line values use a delimiter that cannot occur in them."""
    path = os.environ.get("GITHUB_OUTPUT")
    lines = []
    for k, v in kv.items():
        v = str(v)
        if "\n" in v:
            delim = "PR_REVIEW_EOF_" + os.urandom(8).hex()
            lines.append(f"{k}<<{delim}\n{v}\n{delim}")
        else:
            lines.append(f"{k}={v}")
    text = "\n".join(lines) + "\n"
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else ""
    e = os.environ
    api = Api(e.get("GH_TOKEN", ""), e.get("GITHUB_API_URL", "https://api.github.com"))
    ctx = Ctx()
    if cmd == "start":
        at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        comments = api.get(f"repos/{ctx.repo}/issues/{ctx.pr}/comments", paginate=True)
        n = count_bounces(api, ctx.repo, ctx.pr, comments)
        cap = int(e.get("REVIEW_ESCALATE_AFTER") or ESCALATE_AFTER)
        esc = n >= cap
        if esc:
            print(f"::warning::Blocked {n} times — the reviewer is standing down and escalating.")
            escalate(api, ctx.repo, ctx.pr, n, e.get("PR_TITLE", ""), e.get("PR_URL", ""),
                     ctx.forge_url, ctx.forge_key, comments=comments)
        p, f, src = resolve_models(ctx.forge_url, ctx.forge_key, e.get("PR_AUTHOR", ""),
                                   e.get("PR_TITLE", ""), ctx.repo, ctx.pr)
        print(f"Reviewer models — primary={p} fallback={f} (source: {src})")
        _out(run_started=at, bounces=n, escalate=str(esc).lower(), primary=p, fallback=f)
        return 0
    if cmd == "prepare":
        cfg = repo_config(api, ctx.repo, e.get("BASE_SHA", ""))
        with open(e["PROMPT_FILE"], encoding="utf-8") as fh:
            prompt = build_prompt(fh.read(), cfg)
        _out(prompt=prompt, claude_args=claude_args(e.get("MODEL", ""), cfg),
             python_version=cfg.get("python_version", ""), install=cfg.get("install", ""))
        return 0
    if cmd == "has-verdict":
        v = has_verdict(api, ctx.repo, ctx.pr, ctx.run_started, e.get("ON_MISSING", "fail"))
        print(f"verdict present for this run: {v}")
        _out(has_verdict=v)
        # CREDENTIALS ARE THE ONE FAILURE THE FALLBACK MUST NOT ABSORB. On a Dependabot
        # run GitHub withholds Actions secrets, the token arrives EMPTY, and the action
        # no-ops green. The fallback holds the same empty key, so it cannot cover this —
        # say it here, in a step that is NOT continue-on-error. (It stranded ~35 Dependabot
        # PRs on 2026-08-21 behind two green review checks and one red gate.)
        if v == "false" and e.get("CHECK_CREDENTIALS") == "true" and not e.get("OAUTH"):
            print("::error::NO REVIEWER CREDENTIALS — CLAUDE_CODE_OAUTH_TOKEN is empty, so "
                  "neither model can review. On a Dependabot PR this is expected until the "
                  "secret is added to the repository's DEPENDABOT secret scope (Settings > "
                  "Secrets and variables > Dependabot). This is a missing key, not a "
                  "finding; your code has not been judged.")
            return 1
        return 0
    if cmd == "stamp":
        stamp(ctx, api)
        return 0
    if cmd == "enforce":
        # The allowlist comes from the BASE commit's config, like everything else a PR
        # must not be able to rewrite about its own review. Unreadable config raises, and
        # a crashed gate is a blocked gate.
        cfg = repo_config(api, ctx.repo, e.get("BASE_SHA", ""))
        ctx.allowlist = tuple((cfg.get("override_allowlist") or "").split()) or ctx.allowlist
        return enforce(ctx, api)
    if cmd == "mergeable":
        return mergeable(api, ctx.repo, ctx.pr)
    print(f"usage: pr_review_gate.py start|prepare|has-verdict|stamp|enforce|mergeable (got {cmd!r})")
    return 2


if __name__ == "__main__":
    sys.exit(main())
