"""Attacker-controllable text must never be interpolated into a workflow `run:` body.

WHY
---
`${{ ... }}` is not a variable reference. GitHub substitutes it as LITERAL TEXT into the
script before a shell or interpreter ever starts, so whatever the value contains becomes
part of the program. A pull request title is text an outsider chooses, and the escalation
step in `claude-code-review.yml` used to paste it straight into a `python3 -c "..."`:

    'title': '''${{ github.event.pull_request.title }}'''[:200],

Two independent escapes, both reproduced against the real fragment before this test was
written:

  * a title containing `'''` closes the Python string, and the rest runs as Python;
  * a title containing `"` closes the shell's double quote, and the rest runs as shell.

The step that fires holds `GH_TOKEN` with `pull-requests: write` and `FORGE_API_KEY` in its
environment, so either escape is credential theft, not just a crash. It fires on the 7th
blocking review round — which a PR author reaches by pushing code that keeps failing review,
needing no permission beyond opening a same-repo PR.

THE FIX THIS TEST PROTECTS
--------------------------
Pass the value through `env:` and read it with `os.environ`. An environment value is data:
the shell never parses it as source. That is the only safe shape, and it costs two lines.

WHY A TEST AND NOT JUST THE FIX
-------------------------------
This workflow is copy-pasted across the estate and edited by hand, and the vulnerable line
looks completely ordinary — it reads like string formatting. Nothing about it is visibly
wrong, which is why four repos carried it at once. This test is the thing that notices.
"""
import pathlib
import re

def _workflows_dir():
    """Walk up from this file to the repo root. The test lives in a subdirectory in some
    repos and at the top level in others; hardcoding the depth is how a copy of this file
    ends up scanning an empty directory and passing without checking anything."""
    for d in [pathlib.Path(__file__).resolve()] + list(pathlib.Path(__file__).resolve().parents):
        candidate = d / ".github" / "workflows"
        if candidate.is_dir():
            return candidate
    raise AssertionError("no .github/workflows found above this test — it would scan nothing")


WORKFLOWS = _workflows_dir()

# Contexts whose value an outsider can choose. `github.event.*` covers the PR title, body,
# branch names, commit messages and issue comments; `github.head_ref` is the branch name on
# a fork PR. `github.repository`, `secrets.*` and `steps.*` outputs are NOT here: their
# values are set by us or by GitHub, not by the person opening the PR.
UNTRUSTED = re.compile(r"\$\{\{\s*(github\.event\b|github\.head_ref\b)[^}]*\}\}")

# "file:context" -> why it is not the same class of hole, and what is happening about it.
#
# These are NOT waved through as fine. They are interpolations this scan found which sit
# behind a write-access trigger, so reaching them already requires the thing the PR-title
# hole hands over for free. They are deliberately not changed in the same PR as the
# security fix: promote-to-prod.yml is the production promotion path, and quietly editing
# it to close a low-severity issue risks breaking a promotion a human is relying on. A
# follow-up issue is still to be filed — see the PR description.
#
# Nothing may be added here without a written reason, and
# test_no_acknowledgement_is_speculative deletes entries that stop matching.
ACKNOWLEDGED: dict[str, str] = {
}

# `run: |`, `run: >`, `run: >-`, or a one-line `run: something`.
_RUN_BLOCK = re.compile(r"^(\s*)(?:-\s+)?run:\s*[|>][-+]?\s*$")
_RUN_INLINE = re.compile(r"^(\s*)(?:-\s+)?run:\s*(\S.*)$")


def _run_body_lines(text):
    """[(lineno, line)] for every line that is part of a `run:` script.

    Deliberately line-based rather than a YAML parse: this file must run with nothing
    installed, and a workflow that fails to parse must not silently scan as clean.
    """
    out = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _RUN_BLOCK.match(line)
        if m:
            indent = len(m.group(1))
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent:
                    break
                out.append((i + 1, nxt))
                i += 1
            continue
        m = _RUN_INLINE.match(line)
        if m:
            out.append((i + 1, m.group(2)))
        i += 1
    return out


def _all_offences():
    """[(key, description)] for every untrusted interpolation in a run body, acknowledged
    or not. `key` is "file:context", matching the keys of ACKNOWLEDGED."""
    found = []
    for p in sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml")):
        for lineno, line in _run_body_lines(p.read_text(encoding="utf-8", errors="ignore")):
            for m in UNTRUSTED.finditer(line):
                context = re.sub(r"^\$\{\{\s*|\s*\}\}$", "", m.group(0)).strip()
                found.append((f"{p.name}:{context}",
                              f"{p.name}:{lineno}: {m.group(0)}  in  {line.strip()[:90]}"))
    return found


def _offences():
    return [desc for key, desc in _all_offences() if key not in ACKNOWLEDGED]


def test_no_untrusted_context_is_interpolated_into_a_run_body():
    bad = _offences()
    assert not bad, (
        "attacker-controllable text is substituted into a shell/python script as source "
        "code — a crafted PR title executes on a runner holding GH_TOKEN and "
        "FORGE_API_KEY. Pass it through `env:` and read os.environ instead:\n  "
        + "\n  ".join(bad)
    )


def test_no_acknowledgement_is_speculative():
    """An acknowledgement for an interpolation that no longer exists is a pre-authorised
    hole waiting for someone to reintroduce the line under a name already on the list."""
    live = {key for key, _ in _all_offences()}
    dead = sorted(k for k in ACKNOWLEDGED if k not in live)
    assert not dead, (
        "acknowledgement(s) matching nothing in this repo — the interpolation was fixed or "
        "moved, so delete the entry:\n  " + "\n  ".join(dead)
    )


def test_the_pull_request_title_is_never_acknowledged():
    """The one thing that must never be added to ACKNOWLEDGED to make this test pass.

    Everything on that list is there because reaching it already needs write access. A PR
    title does not: that is the entire severity difference, and the reason this file exists.
    """
    smuggled = [k for k in ACKNOWLEDGED if "pull_request.title" in k or "pull_request.body" in k]
    assert not smuggled, (
        "a pull_request title/body interpolation has been added to the acknowledged list — "
        "that is the exact hole this test was written for, and it is reachable by anyone "
        "who can open a PR:\n  " + "\n  ".join(smuggled)
    )


def test_the_escalation_step_still_sends_the_title_from_the_environment():
    """Names the specific step, so 'no offences found' cannot mean 'the step moved away'.

    The scanner reports success just as loudly when the file it was written for has been
    renamed or the payload dropped. This asserts the safe shape is actually present.
    """
    wf = WORKFLOWS / "claude-code-review.yml"
    assert wf.exists(), "claude-code-review.yml is gone — this test no longer checks anything"
    text = wf.read_text(encoding="utf-8", errors="ignore")
    assert "os.environ['TITLE']" in text, (
        "the escalation payload no longer reads the PR title from the environment — if the "
        "title came back as ${{ }} interpolation, the injection is back"
    )
    assert re.search(r"^\s+TITLE:\s*\$\{\{\s*github\.event\.pull_request\.title\s*\}\}\s*$",
                     text, re.M), (
        "TITLE is not bound in an `env:` block — os.environ['TITLE'] would raise KeyError "
        "and the escalation call would fail"
    )


def test_the_scanner_catches_the_line_that_was_actually_vulnerable():
    """A positive control. Every other assertion here is negative — does bad input fail? —
    and a scanner whose regex or block-detection quietly stops matching passes all of them
    while checking nothing. This feeds it the exact line this repo shipped."""
    vulnerable = (
        "      - name: Escalate\n"
        "        run: |\n"
        "          curl -d \"$(python3 -c \"\n"
        "          print({\n"
        "            'title': '''${{ github.event.pull_request.title }}'''[:200],\n"
        "          })\n"
        "          \")\"\n"
    )
    hits = [l for _, l in _run_body_lines(vulnerable) if UNTRUSTED.search(l)]
    assert hits, "the scanner no longer detects the original vulnerable line"


def test_env_bindings_and_if_conditions_are_not_flagged():
    """The safe shapes must stay usable, or the next person routes around the test.

    `env:` values and `if:` expressions are evaluated by GitHub as data and never parsed as
    shell source — flagging them would make the only correct fix look like a violation.
    """
    safe = (
        "      - name: Escalate\n"
        "        if: github.event.pull_request.draft == false\n"
        "        env:\n"
        "          TITLE: ${{ github.event.pull_request.title }}\n"
        "        run: |\n"
        "          python3 -c \"import os; print(os.environ['TITLE'])\"\n"
    )
    hits = [l for _, l in _run_body_lines(safe) if UNTRUSTED.search(l)]
    assert not hits, f"the safe env-passing shape is being reported as a violation: {hits}"


def _main():
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}\n     {e}")
    print(f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    _main()
