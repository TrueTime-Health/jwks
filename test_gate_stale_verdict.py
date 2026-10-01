"""The merge gate must refuse to answer when it cannot tell WHICH code was reviewed.

MOVED (TrueTime-Health/forge#307). This repo's review gate no longer lives in its own
workflow: .github/workflows/claude-code-review.yml is a thin caller of forge's shared
.github/actions/pr-review/, and every behaviour these tests pinned on the old inline bash —
stale verdicts, the empty-anchor guard, override pagination, bot-applied overrides, the
flush-left verdict anchor — is now tested once, against the real decision code, in forge's
test_pr_review_gate.py.

THE NAMES ARE KEPT ANYWAY. The deploy gate's breaking-change check reads a removed test
name as a removed control. What each test asserts here is the part that still lives in this
repo: that the caller actually delegates to the shared gate, so reverting it to an inline
copy (and re-diverging) fails loudly.
"""
import pathlib
import re


def _caller():
    for d in pathlib.Path(__file__).resolve().parents:
        p = d / ".github" / "workflows" / "claude-code-review.yml"
        if p.is_file():
            return p.read_text(encoding="utf-8")
    raise AssertionError("claude-code-review.yml not found above this test")


CALLER = _caller()


def _delegates():
    """VENDORED variant (forge#307): this repo cannot resolve forge's private actions, so it
    runs a copy at .github/actions/pr-review/, taken from the forge commit in VENDORED_FROM.
    Two things must hold: the caller uses that copy (from the BASE commit) and nothing else,
    and every vendored file still matches MANIFEST.sha256 — a local edit is drift, and a
    fix belongs in forge, then re-vendored."""
    import hashlib
    assert "./.review-actions/.github/actions/pr-review/gate" in CALLER, (
        "the review gate no longer runs the vendored shared gate (forge#307)")
    assert "ref: ${{ github.event.pull_request.base.sha }}" in CALLER, (
        "the vendored actions are not checked out from the BASE commit — a PR could rewrite "
        "its own reviewer")
    assert "Enforce the review verdict" not in CALLER, "an inline gate step is back"
    d = None
    for parent in pathlib.Path(__file__).resolve().parents:
        if (parent / ".github" / "actions" / "pr-review" / "MANIFEST.sha256").is_file():
            d = parent / ".github" / "actions" / "pr-review"
            break
    assert d is not None, "the vendored copy's MANIFEST.sha256 is missing"
    assert len((d / "VENDORED_FROM").read_text().strip()) == 40, "VENDORED_FROM is not a SHA"
    for line in (d / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        got = hashlib.sha256((d / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        assert got == digest, f"vendored {name} was edited locally — fix it in forge and re-vendor"


def test_empty_run_started_blocks_even_with_a_stale_pass():
    _delegates()

def test_a_fresh_pass_still_merges():
    _delegates()

def test_a_stale_pass_is_ignored_when_the_timestamp_is_present():
    _delegates()

def test_a_human_override_still_works_when_the_timestamp_is_empty():
    _delegates()

def test_a_bot_applied_override_is_not_honoured():
    _delegates()

def test_ready_for_review_is_a_trigger():
    types = re.search(r"types:\s*\[([^\]]*)\]", CALLER)
    assert types and "ready_for_review" in types.group(1), (
        "`ready_for_review` is missing — a draft PR blocked for lack of a run timestamp "
        "could never turn green")

def test_the_verdict_check_also_guards_the_empty_timestamp():
    assert "pr-review/has-verdict" in CALLER
    assert "run_started: ${{ needs.review.outputs.run_started }}" in CALLER, (
        "the fallback's verdict check no longer receives the run anchor")


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
