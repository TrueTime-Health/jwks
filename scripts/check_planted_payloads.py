#!/usr/bin/env python3
"""Planted-payload check.

WHY THIS EXISTS
---------------
On 2026-08-01 a commit titled "fix(identity): make the bot-identity hook
self-contained" added 23 files to three repos in 53 seconds. Among them:

  public/fonts/fa-solid-400.woff2   9,129 bytes, no wOF2 magic - an obfuscated
                                    Node loader that reads C2 addresses out of an
                                    Ethereum mainnet transaction, fetches
                                    XOR-encrypted second-stage code, and runs it
                                    via eval() and a detached hidden `node -e`.
  .vscode/tasks.json                runOn folderOpen, hide true, reveal never -
                                    its only job is to run that file.
  .vscode/settings.json             task.allowAutomaticTasks true - suppresses the
                                    editor prompt, so it ran with no confirmation.

It spread to seven repos and sat on the public `jwks` repo for 14 days. Nothing in
the pipeline looked at it: not the tests, linters, gitleaks, pip-audit, the Forge
gate, nor check_build_config_integrity.py, whose watch list is seven build-config
files and covers neither editor config nor assets.

WHAT THIS CHECKS (positive assertions, not absence)
---------------------------------------------------
1. Every file with an asset extension MUST begin with that format's magic bytes.
   A size or shape check cannot catch this - the payload is padded whitespace and
   looks unremarkable by size. The magic byte is the thing that is true of a real
   font and false of a script.
2. No editor config may auto-run anything (runOn: folderOpen).
3. No editor config may suppress the auto-run confirmation
   (task.allowAutomaticTasks).
4. No .gitignore may un-ignore .vscode/ - that hunk is what let a blanket
   `git add -A` sweep the trigger into the repo in the first place.

Checks 2 and 3 decode \\uXXXX escapes before matching, because VS Code parses
these files as JSONC and resolves the escapes; "folder\\u004Fpen" is identical to
"folderOpen" in the editor and would otherwise sail through. The adversary this
targets already ships escape-obfuscated payloads.

NOT COVERED
-----------
.svg is scriptable and has no magic bytes, so it is outside this control. Do not
read a pass here as "no executable content in assets".

It always prints how many files it actually inspected. A checker that scans
nothing prints the same "OK" as one that finds nothing, and that difference has
cost us before.

Exit 0 = clean, exit 1 = something needs a human look.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

# Formats whose first bytes are fixed by spec. Value = tuple of accepted magics;
# empty tuple = counted but unverifiable.
MAGIC: dict[str, tuple[bytes, ...]] = {
    ".woff2": (b"wOF2",),
    ".woff": (b"wOFF",),
    # 0x00010000 is the common one; Apple's variant uses 'true', and 'ttcf' is a
    # TrueType collection. All three are legitimate.
    ".ttf": (b"\x00\x01\x00\x00", b"true", b"ttcf"),
    ".otf": (b"OTTO",),
    ".png": (b"\x89PNG",),
    ".gif": (b"GIF8",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".pdf": (b"%PDF",),
    ".ico": (b"\x00\x00\x01\x00",),
    ".eot": (),  # no stable magic
}

# Only used when the tree is not a git checkout. Inside a checkout we enumerate
# tracked files instead, so a payload committed under dist/ or .next/ is still seen.
FALLBACK_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
                      ".mypy_cache", ".pytest_cache", "site-packages"}

# Shapes a disguised asset needs in order to do anything.
PAYLOAD_SHAPES: list[tuple[str, str]] = [
    (r"\bchild_process\b|\bspawn\s*\(|\bexecSync\s*\(", "child process spawn"),
    (r"\beval\s*\(|\bnew\s+Function\s*\(", "dynamic code execution"),
    (r"0x[0-9a-fA-F]{40}", "Ethereum address (blockchain C2 dead-drop)"),
    (r"(?:\\u00[0-9a-fA-F]{2}){8,}", "long Unicode-escape run (obfuscation)"),
    (r"\brequire\s*\(", "require() call"),
]

_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")

# Bytes that appear in ordinary text: printable ASCII plus tab/newline/CR.
_TEXT_BYTES = frozenset(range(0x20, 0x7f)) | {0x09, 0x0a, 0x0d}


def _is_mostly_binary(data: bytes) -> bool:
    """True if the sample looks like real binary (a font/image), not text/code.

    A genuine woff2/otf/gif is compressed or otherwise binary right after its
    header, so it is dense with non-text bytes. A JavaScript polyglot that merely
    prepends the magic string is almost entirely printable. Threshold is generous:
    real binary assets run far above it, source code far below.
    """
    if not data:
        return False
    nontext = sum(1 for b in data if b not in _TEXT_BYTES)
    return nontext / len(data) > 0.20


def decode_json_escapes(text: str) -> str:
    """Resolve \\uXXXX the way a JSONC parser would, so escaped keys cannot hide."""
    return _ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)


def list_files(root: pathlib.Path) -> tuple[list[pathlib.Path], str]:
    """Prefer git-tracked files: it scans exactly what is committed, with no
    skip-list guesswork, so a payload committed under dist/ or .next/ is still seen."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                             capture_output=True, timeout=60)
        if out.returncode == 0:
            names = [n for n in out.stdout.decode("utf8", "replace").split("\0") if n]
            if names:
                return [root / n for n in names], "git-tracked"
    except Exception:  # noqa: BLE001 - not a checkout, or git unavailable
        pass

    found: list[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in FALLBACK_SKIP_DIRS]
        for name in filenames:
            found.append(pathlib.Path(dirpath) / name)
    return found, "filesystem walk"


def check(root: pathlib.Path) -> tuple[list[str], int, int, str]:
    findings: list[str] = []
    inspected = 0
    files, how = list_files(root)

    for path in files:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = str(path)
        ext = path.suffix.lower()

        # 1. assets must match their declared format
        if ext in MAGIC:
            inspected += 1
            magics = MAGIC[ext]
            if not magics:
                continue
            try:
                with path.open("rb") as fh:
                    blob = fh.read(8192)
                size = path.stat().st_size
            except OSError as exc:
                findings.append(f"{rel}: unreadable ({exc})")
                continue
            head = blob[:8]
            matched = next((m for m in magics if head.startswith(m)), None)

            # A matched magic is NOT sufficient when the magic is printable ASCII.
            # wOF2, wOFF, OTTO, true, ttcf, GIF8 are all valid JavaScript openings:
            # `wOF2=1;const cp=require("child_process");...` starts with the exact
            # magic AND runs under node. That is the GIF89a-polyglot technique, and
            # it defeats a pure startswith() check. A genuine font/image is BINARY
            # immediately after its header, so a matched-magic file whose body is
            # mostly printable text is a polyglot, not an asset.
            reason = None
            if matched is None:
                reason = (f"claims {ext} but starts with {head[:4]!r} "
                          f"({size} bytes) - not a real {ext.lstrip('.')}")
            elif all(0x20 <= b < 0x7f for b in matched) and not _is_mostly_binary(blob):
                reason = (f"starts with the {ext} magic {matched!r} but the body is "
                          f"text, not binary - a real {ext.lstrip('.')} is binary "
                          f"after its header (magic-byte polyglot)")

            if reason:
                findings.append(f"{rel}: {reason}")
                try:
                    text = path.read_text(encoding="utf8", errors="replace")
                except OSError:
                    continue
                for pattern, label in PAYLOAD_SHAPES:
                    if re.search(pattern, text):
                        findings.append(f"    ^ contains {label}")

        # 2 & 3. editor config must not auto-run, and must not silence the prompt.
        #
        # All comparisons are lowercased. The CI runner is Linux (case-sensitive), but
        # the fleet develops on Windows and macOS, where the filesystem is
        # case-INSENSITIVE: a committed `.VSCode/Tasks.json` is invisible to a
        # case-sensitive match here, yet resolves to `.vscode/tasks.json` and auto-runs
        # in the editor. .code-workspace files carry the same tasks/settings blocks and
        # runOn support, at any path, so they are checked wherever they sit.
        name = path.name.lower()
        parts_lower = [p.lower() for p in path.parts]
        is_vscode_json = name in {"tasks.json", "settings.json", "launch.json"} \
            and ".vscode" in parts_lower
        is_workspace = path.suffix.lower() == ".code-workspace"
        if is_vscode_json or is_workspace:
            inspected += 1
            try:
                raw = path.read_text(encoding="utf8", errors="replace")
            except OSError as exc:
                # Symmetric with the asset branch above: fail closed, never skip.
                findings.append(f"{rel}: unreadable ({exc})")
                continue
            text = decode_json_escapes(raw)
            # folderOpen is runOn's only dangerous value, so flag the key itself.
            if re.search(r"[\"']runOn[\"']", text) or "folderOpen" in text:
                findings.append(f"{rel}: declares a runOn trigger (auto-runs a task)")
            if "allowAutomaticTasks" in text:
                findings.append(
                    f"{rel}: sets task.allowAutomaticTasks (suppresses the "
                    f"auto-run confirmation prompt)")

        # 4. .gitignore must not un-ignore .vscode/ in any of its forms
        if name == ".gitignore":
            inspected += 1
            try:
                text = path.read_text(encoding="utf8", errors="replace")
            except OSError as exc:
                findings.append(f"{rel}: unreadable ({exc})")
                continue
            # Catches !.vscode, !/.vscode/, !.vscode/**, !.vscode/tasks.json - the
            # per-file form is the documented way to re-include under an ignored
            # directory, so it is the likeliest hunk, not an edge case.
            for lineno, line in enumerate(text.splitlines(), start=1):
                if re.match(r"^\s*!.*\.vscode", line, re.IGNORECASE):
                    findings.append(
                        f"{rel}:{lineno}: un-ignores .vscode ({line.strip()!r})")

    return findings, inspected, len(files), how


def main() -> int:
    root = pathlib.Path(os.environ.get("PAYLOAD_CHECK_ROOT", ".")).resolve()
    if not root.is_dir():
        print(f"Planted-payload check: ERROR - {root} is not a directory")
        return 2

    findings, inspected, total, how = check(root)

    print(f"Planted-payload check: {how}, {total} file(s) under {root.name}, "
          f"inspected {inspected} asset/config candidate(s)")

    if not findings:
        print("Planted-payload check: OK")
        return 0

    print("\nPlanted-payload check: FAILED\n")
    for f in findings:
        print(f"  {f}")
    print(
        "\nAn asset that is not the format it claims to be is the folderOpen "
        "dropper's signature.\nDo not 'fix' this by renaming the file or widening "
        "the check. Read the file, and if it\nis genuinely a legitimate asset, "
        "correct it so it carries real format bytes."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
