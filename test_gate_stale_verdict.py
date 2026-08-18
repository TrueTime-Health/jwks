"""The merge gate must refuse to answer when it cannot tell WHICH code was reviewed.

WHY
---
The gate decides "may this merge?" by reading the last `claude[bot]` comment posted *since
this run started*. That scoping is the whole control: without it, a `REVIEW: PASS` left on an
earlier commit approves whatever is pushed afterwards.

The timestamp comes from the `review` job (`Mark run start`). Two ordinary situations leave
it EMPTY:

  * the PR is a **draft** — the review job is skipped by `if: draft == false`, but the gate
    still runs because it is `if: always()`, so `needs.review.outputs.run_started` is "";
  * the review job **dies before** `Mark run start` — e.g. a `gh api` flake in "Count prior
    blocking reviews" aborts it under `set -euo pipefail`.

An empty timestamp makes the filter `select(.created_at >= "")`, and **every** string is
`>= ""`. So the filter matches every `claude[bot]` comment ever posted on the PR, the gate
reads a stale `PASS` from an earlier commit, and unreviewed code merges behind a green check.

The concrete draft-PR chain: get a PASS, convert to draft, push unreviewed commits (the review
job skips, the gate passes on the stale verdict), then mark ready for review.

WHY THE TRIGGER IS PART OF THE FIX
----------------------------------
Failing closed on an empty timestamp strands draft PRs red — correctly — but `ready_for_review`
was missing from the workflow's trigger types, so marking a PR ready re-ran nothing and it
stayed red forever. The guard and the trigger only work as a pair; that is why both are asserted
here.

HOW THIS IS TESTED
------------------
Not by reading the YAML for a magic string — by **extracting the gate's actual shell body and
running it** against a stubbed `gh`, then asserting on the exit code. A test that greps for
`[ -z "$RUN_STARTED" ]` would pass on a version where the guard sits in the wrong place.

Placement matters and is asserted: the guard must come AFTER the two override doors. A PR that
edits this workflow is skipped by the platform and can never earn a verdict, so if the guard
ran first, the human `review:override` label would stop working and the reviewer workflow
would become permanently unmergeable — the deadlock the escape hatch exists to prevent.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

REPO = "TrueTime-Health/testrepo"
STEP = "Enforce the review verdict"


def _workflow():
    for d in pathlib.Path(__file__).resolve().parents:
        p = d / ".github" / "workflows" / "claude-code-review.yml"
        if p.is_file():
            return p
    raise AssertionError("claude-code-review.yml not found above this test")


WORKFLOW = _workflow()


def _step_body(step_name):
    """The `run:` script of a named step, with ${{ }} expressions resolved for local use.

    Extracted from the real file rather than duplicated here — a copy would keep passing
    after the workflow changed, which is the failure mode this test exists to avoid.
    """
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip() == f"- name: {step_name}"), None)
    assert start is not None, f"step {step_name!r} not found — the test no longer checks anything"
    run_i = next(i for i in range(start, len(lines)) if re.match(r"^\s*run:\s*\|", lines[i]))
    indent = len(lines[run_i]) - len(lines[run_i].lstrip())
    body = []
    for l in lines[run_i + 1:]:
        if l.strip() and (len(l) - len(l.lstrip())) <= indent:
            break
        body.append(l[indent + 2:] if len(l) > indent + 2 else "")
    text = "\n".join(body)
    # env: values are supplied as real environment variables by the runner; the only
    # interpolation left inside the body is the repo slug.
    text = text.replace("${{ github.repository }}", REPO)
    assert "${{" not in text, f"unresolved expression left in the extracted body:\n{text}"
    return text


SHIM = r'''#!/usr/bin/env python
"""Stand-in for `gh api`, faithful to the two jq filters the gate actually builds.

The comment filter is read out of the real --jq argument, INCLUDING the timestamp the
workflow interpolated. Python and jq both compare strings lexicographically, and every
string is >= "", so an empty timestamp reproduces the real fail-open here exactly as it
would on a runner. Nothing about the defect is emulated away.
"""
import json, os, re, sys

args = sys.argv[1:]
data = json.load(open(os.environ["FIXTURE"]))
jqf = args[args.index("--jq") + 1] if "--jq" in args else ""
path = next((a for a in args if a.startswith("repos/")), "")

if "/timeline" in path:
    ev = [e for e in data["timeline"]
          if e.get("event") == "labeled" and e.get("label", {}).get("name") == "review:override"]
    if ev and ev[-1].get("actor", {}).get("type") != "Bot":
        print(ev[-1]["actor"]["login"])
    sys.exit(0)

if "/comments" in path:
    m = re.search(r'\.created_at >= "([^"]*)"', jqf)
    ts = m.group(1) if m else ""
    sel = [c for c in data["comments"]
           if c["user"]["login"] == "claude[bot]" and c["created_at"] >= ts]
    print(sel[-1]["body"] if sel else "")
    sys.exit(0)

sys.exit(0)
'''

OLD = "2026-08-01T00:00:00Z"
NEW = "2026-08-17T12:00:00Z"
RUN_AT = "2026-08-17T11:00:00Z"


def _run(fixture, run_started, forge_url=""):
    """Execute the real gate body; return (exit_code, output)."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="gatetest_"))
    try:
        (tmp / "fixture.json").write_text(json.dumps(fixture), encoding="utf-8")
        (tmp / "gh.py").write_text(SHIM, encoding="utf-8")
        # `gh` must be callable by bare name from bash.
        (tmp / "gh").write_text(f'#!/bin/sh\nexec python "{tmp.as_posix()}/gh.py" "$@"\n',
                                encoding="utf-8")
        os.chmod(tmp / "gh", 0o755)
        (tmp / "script.sh").write_text(_step_body(STEP), encoding="utf-8", newline="\n")
        env = dict(os.environ,
                   PATH=f"{tmp.as_posix()}{os.pathsep}{os.environ['PATH']}",
                   FIXTURE=str(tmp / "fixture.json"),
                   RUN_STARTED=run_started, GH_TOKEN="x", PR="1",
                   FORGE_URL=forge_url, FORGE_API_KEY="")
        r = subprocess.run(["bash", str(tmp / "script.sh")],
                           capture_output=True, text=True, env=env)
        return r.returncode, (r.stdout + r.stderr)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _fixture(comments, timeline=()):
    return {"comments": list(comments), "timeline": list(timeline)}


def _comment(body, at):
    return {"user": {"login": "claude[bot]"}, "body": body, "created_at": at}


STALE_PASS = _fixture([_comment("REVIEW: PASS looks good", OLD)])


def test_empty_run_started_blocks_even_with_a_stale_pass():
    """THE BUG. Draft PR or a review job that died: no timestamp, a PASS from an earlier
    commit still in history. The gate must not treat that as approval of this code."""
    code, out = _run(STALE_PASS, run_started="")
    assert code != 0, (
        "GATE FAILED OPEN — with no run-start timestamp the filter matched a PASS left on "
        "an earlier commit and unblocked the merge. Unreviewed code would ship behind a "
        f"green check.\n{out}"
    )


def test_a_fresh_pass_still_merges():
    """The guard must not break the ordinary path, or it gets reverted within a day."""
    fresh = _fixture([_comment("REVIEW: PASS looks good", NEW)])
    code, out = _run(fresh, run_started=RUN_AT)
    assert code == 0, f"a genuine PASS from this run no longer unblocks the merge:\n{out}"


def test_a_stale_pass_is_ignored_when_the_timestamp_is_present():
    """The scoping itself — a PASS older than this run is not this run's verdict."""
    code, out = _run(STALE_PASS, run_started=RUN_AT)
    assert code != 0, f"a PASS predating this run was accepted as its verdict:\n{out}"


def test_a_human_override_still_works_when_the_timestamp_is_empty():
    """PLACEMENT. The guard must sit AFTER the override doors.

    A PR that edits this workflow is skipped by the platform and can never earn a verdict;
    the human `review:override` label is its only exit. If the empty-timestamp guard ran
    first it would swallow that exit and make the reviewer workflow permanently unmergeable
    — the deadlock the escape hatch was written to prevent. This is the test that fails if
    the guard is pasted at the top of the step.
    """
    ovr = _fixture([_comment("REVIEW: PASS", OLD)],
                   timeline=[{"event": "labeled", "label": {"name": "review:override"},
                              "actor": {"login": "sumitcareteams", "type": "User"}}])
    code, out = _run(ovr, run_started="")
    assert code == 0, (
        "a human review:override no longer unblocks a PR with no run timestamp — the "
        f"reviewer workflow's own PRs are now unmergeable by anyone:\n{out}"
    )


def test_a_bot_applied_override_is_not_honoured():
    """The agent must not be able to wave its own work through by labelling its own PR."""
    botovr = _fixture([_comment("REVIEW: PASS", OLD)],
                      timeline=[{"event": "labeled", "label": {"name": "review:override"},
                                 "actor": {"login": "truetime-helper[bot]", "type": "Bot"}}])
    code, out = _run(botovr, run_started="")
    assert code != 0, f"a BOT-applied override unblocked the merge:\n{out}"


def test_ready_for_review_is_a_trigger():
    """Without it the guard strands draft PRs red forever.

    Failing closed on a draft is correct, but the recovery has to exist: marking the PR
    ready must re-run the review. `ready_for_review` is not in the default set for
    `pull_request`, so its absence is silent.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    types = re.search(r"types:\s*\[([^\]]*)\]", text)
    assert types, "no `types:` list on the pull_request trigger"
    assert "ready_for_review" in types.group(1), (
        "`ready_for_review` is missing from the trigger types — a draft PR blocked by the "
        "empty-timestamp guard can never turn green, because marking it ready re-runs "
        f"nothing. Found: [{types.group(1).strip()}]"
    )


def test_the_verdict_check_also_guards_the_empty_timestamp():
    """Same hole, second reader: the fallback's `verdict_check` uses the same filter."""
    body = _step_body("Did the preferred model return a verdict?")
    assert re.search(r'-z\s+"\$\{RUN_STARTED:-\}"|-z\s+"\$RUN_STARTED"', body), (
        "verdict_check reads the run-start timestamp but does not fail closed when it is "
        "empty — it would decide 'the primary already answered' from a comment on an "
        "earlier commit and skip the fallback"
    )


def _main():
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}\n     {str(e)[:400]}")
    print(f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    _main()
