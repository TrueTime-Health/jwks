"""Behaviour lock for the planted-payload check.

The evasion cases here are not hypothetical: findings 2, 3 and 4 of the review on
truetime-wisdom-base#22 were each a real bypass in the first version of this script.
They are pinned so they cannot creep back.

Every assertion is on the EXIT CODE, because that is what CI reads.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

CHECKER = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "check_planted_payloads.py"


def run(root: pathlib.Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PAYLOAD_CHECK_ROOT=str(root))
    return subprocess.run([sys.executable, str(CHECKER)],
                          capture_output=True, text=True, env=env)


@pytest.fixture
def tree(tmp_path: pathlib.Path) -> pathlib.Path:
    """A clean tree that still contains a GENUINE asset.

    Without a real font here, a pass would only prove 'nothing to look at'.
    """
    fonts = tmp_path / "public" / "fonts"
    fonts.mkdir(parents=True)
    (fonts / "fa-solid-900.woff2").write_bytes(b"wOF2" + b"\x00" * 64)
    (tmp_path / "README.md").write_text("# fixture\n")
    (tmp_path / ".gitignore").write_text("# IDE\n.vscode/\nnode_modules/\n")
    return tmp_path


def test_clean_tree_passes(tree: pathlib.Path) -> None:
    r = run(tree)
    assert r.returncode == 0, r.stdout
    # The genuine font and the .gitignore both count. What matters is that the pass
    # came from looking at something, not from finding nothing to look at.
    assert "inspected 0" not in r.stdout, "a pass over an empty scan proves nothing"


def test_reports_zero_honestly(tmp_path: pathlib.Path) -> None:
    r = run(tmp_path)
    assert r.returncode == 0
    assert "inspected 0" in r.stdout, "an empty scan must say so, not just print OK"


def test_disguised_asset_is_caught(tree: pathlib.Path) -> None:
    (tree / "public" / "fonts" / "fa-solid-400.woff2").write_text(
        "    " * 40 + "const r=require('http');eval(x);")
    r = run(tree)
    assert r.returncode == 1
    assert "not a real woff2" in r.stdout


def test_apple_truetype_variants_are_accepted(tree: pathlib.Path) -> None:
    """'true' and 'ttcf' are legitimate .ttf magics; flagging them is a false positive."""
    (tree / "public" / "fonts" / "apple.ttf").write_bytes(b"true" + b"\x00" * 32)
    (tree / "public" / "fonts" / "coll.ttf").write_bytes(b"ttcf" + b"\x00" * 32)
    assert run(tree).returncode == 0


def test_magic_byte_polyglot_is_caught(tree: pathlib.Path) -> None:
    """Review finding (pcr#51, DTC#76): wOF2/OTTO/GIF8/true are valid JS openings.
    A file that starts with the exact magic but is really a script must be caught,
    even though startswith() passes."""
    (tree / "public" / "fonts" / "poly.woff2").write_text(
        'wOF2=1;const cp=require("child_process");cp.spawn("node",["-e","x"]);')
    r = run(tree)
    assert r.returncode == 1, "a magic-prefixed JS polyglot must not pass"
    assert "polyglot" in r.stdout


def test_true_prefixed_ttf_polyglot_is_caught(tree: pathlib.Path) -> None:
    (tree / "public" / "fonts" / "poly.ttf").write_text(
        'true;eval(require("http"));' + "// padding " * 20)
    assert run(tree).returncode == 1


def test_genuine_binary_font_with_ascii_magic_still_passes(tree: pathlib.Path) -> None:
    """The guard must not false-positive a REAL woff2 (ASCII magic, binary body)."""
    real = b"wOF2" + bytes(range(256)) * 8  # binary after the header
    (tree / "public" / "fonts" / "real.woff2").write_bytes(real)
    assert run(tree).returncode == 0, "a genuine binary font must not be flagged"


def test_folder_open_trigger_is_caught(tree: pathlib.Path) -> None:
    vs = tree / ".vscode"
    vs.mkdir()
    (vs / "tasks.json").write_text(
        '{"tasks":[{"command":"node x","runOptions":{"runOn":"folderOpen"}}]}')
    assert run(tree).returncode == 1


def test_escaped_folder_open_is_caught(tree: pathlib.Path) -> None:
    """Review finding 2: VS Code decodes \\uXXXX, so a raw grep misses this."""
    vs = tree / ".vscode"
    vs.mkdir()
    (vs / "tasks.json").write_text(
        '{"tasks":[{"runOptions":{"runOn":"folder\\u004Fpen"}}]}')
    r = run(tree)
    assert r.returncode == 1, "escaped folderOpen must not slip through"


def test_escaped_allow_automatic_tasks_is_caught(tree: pathlib.Path) -> None:
    vs = tree / ".vscode"
    vs.mkdir()
    (vs / "settings.json").write_text('{"task.allowAutomatic\\u0054asks": true}')
    assert run(tree).returncode == 1


@pytest.mark.parametrize("hunk", [
    "!.vscode",
    "!/.vscode/",
    "!.vscode/**",
    "!.vscode/tasks.json",  # the DOCUMENTED way to re-include under an ignored dir
])
def test_gitignore_unignore_forms_are_caught(tree: pathlib.Path, hunk: str) -> None:
    """Review finding 3: only the bare form was matched before."""
    (tree / ".gitignore").write_text(f"# IDE\n.vscode/\n{hunk}\n")
    r = run(tree)
    assert r.returncode == 1, f"{hunk!r} must be flagged"


def test_case_variant_vscode_dir_is_caught(tree: pathlib.Path) -> None:
    """Review finding (forge round 3): the CI runner is case-sensitive, but the fleet
    develops on Windows/macOS where `.VSCode/Tasks.json` resolves and auto-runs."""
    vs = tree / ".VSCode"
    vs.mkdir()
    (vs / "Tasks.json").write_text(
        '{"tasks":[{"command":"node x","runOptions":{"runOn":"folderOpen"}}]}')
    r = run(tree)
    assert r.returncode == 1, "a case-variant .vscode trigger must not slip through"


def test_case_variant_gitignore_unignore_is_caught(tree: pathlib.Path) -> None:
    (tree / ".gitignore").write_text("# IDE\n.vscode/\n!.VSCODE/tasks.json\n")
    assert run(tree).returncode == 1


def test_code_workspace_auto_run_is_caught(tree: pathlib.Path) -> None:
    """A .code-workspace carries the same tasks/runOn blocks, at any path."""
    (tree / "project.code-workspace").write_text(
        '{"folders":[],"tasks":{"tasks":[{"runOptions":{"runOn":"folderOpen"}}]}}')
    assert run(tree).returncode == 1


def test_ordinary_gitignore_is_not_flagged(tree: pathlib.Path) -> None:
    (tree / ".gitignore").write_text(
        "node_modules/\n.vscode/\n!important.txt\n*.pyc\n")
    assert run(tree).returncode == 0


def test_payload_committed_under_a_build_dir_is_still_seen(tree: pathlib.Path) -> None:
    """Review finding 4: dist/ and .next/ used to be pruned unconditionally."""
    dist = tree / "dist"
    dist.mkdir()
    (dist / "logo.png").write_text("    " + "const {spawn}=require('child_process');")
    r = run(tree)
    assert r.returncode == 1, "a payload committed under dist/ must not be skipped"


# ---------------------------------------------------------------------------
# The tests above run against a plain tmp_path, which is not a git repo, so they
# all exercise the FILESYSTEM-WALK fallback. CI runs inside a checkout and takes
# the git-tracked path instead — a different enumerator, and until now the one
# with no coverage at all. These pin that path specifically.
# ---------------------------------------------------------------------------

def _git_repo(root: pathlib.Path) -> bool:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "fixture"]):
        p = subprocess.run(["git", "-C", str(root), *args],
                           capture_output=True, env=env)
        if p.returncode != 0:
            return False
    return True


@pytest.fixture
def git_tree(tree: pathlib.Path) -> pathlib.Path:
    if not _git_repo(tree):
        pytest.skip("git unavailable")
    return tree


def test_git_tracked_mode_is_actually_used(git_tree: pathlib.Path) -> None:
    """Guard the guard: if this says 'filesystem walk', the tests below prove nothing."""
    r = run(git_tree)
    assert "git-tracked" in r.stdout, r.stdout
    assert r.returncode == 0


def test_git_tracked_mode_catches_a_disguised_asset(git_tree: pathlib.Path) -> None:
    (git_tree / "public" / "fonts" / "fa-solid-400.woff2").write_text(
        "    " * 40 + "eval(require('http'));")
    subprocess.run(["git", "-C", str(git_tree), "add", "-A"], capture_output=True)
    r = run(git_tree)
    assert "git-tracked" in r.stdout
    assert r.returncode == 1


def test_git_tracked_mode_sees_a_payload_under_dist(git_tree: pathlib.Path) -> None:
    """dist/ is not skipped, and being tracked is what makes it visible."""
    dist = git_tree / "dist"
    dist.mkdir()
    (dist / "logo.png").write_text("    " + "const {spawn}=require('child_process');")
    subprocess.run(["git", "-C", str(git_tree), "add", "-Af"], capture_output=True)
    r = run(git_tree)
    assert "git-tracked" in r.stdout
    assert r.returncode == 1


def test_untracked_file_is_not_scanned_in_git_mode(git_tree: pathlib.Path) -> None:
    """Documents the trade-off: git-tracked scans what is COMMITTED, which is the
    thing that ships. An untracked scratch file is deliberately out of scope."""
    (git_tree / "public" / "fonts" / "scratch.woff2").write_text("not a font at all")
    r = run(git_tree)
    assert r.returncode == 0, "untracked files are out of scope in git-tracked mode"
