#!/usr/bin/env python3
"""Recall's PreToolUse guard.

- Denies Claude's direct edits to a vault's .srs/ folder: only srs.py writes there.
- While a Recall command has a session open, lets Claude edit the vault's notes
  without a permission prompt and records each file, so the command can show
  exactly those files as a git diff and commit or discard them. The git diff is
  the review, so it works the same in every permission mode.
- Lets Bash run this plugin's own srs.py without a prompt, when the command is
  exactly that script and nothing else.

Anything else gets no opinion and follows the normal permission flow. Outside a
vault where Recall is set up, the guard does nothing.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

SRS_DIR = ".srs"
OPERATORS = set("();<>|&")


def decide(decision: str, reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))


def find_vault(start: Path):
    cur = start
    for folder in [cur, *cur.parents]:
        if (folder / SRS_DIR / "config.json").is_file():
            return folder
    return None


def is_own_script(command: str, plugin_root: str) -> bool:
    """True only for `python3 <plugin>/scripts/srs.py ...` with no shell operators."""
    if not command or "`" in command or "$(" in command or "\n" in command.strip():
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    if len(tokens) < 2 or any(t and set(t) <= OPERATORS for t in tokens):
        return False
    if os.path.basename(tokens[0]) not in ("python3", "python"):
        return False
    expected = os.path.realpath(os.path.join(plugin_root, "scripts", "srs.py"))
    return os.path.realpath(tokens[1]) == expected


def record_touched(session_file: Path, rel: str) -> None:
    try:
        session = json.loads(session_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    touched = session.setdefault("touched", [])
    if rel not in touched:
        touched.append(rel)
        tmp = session_file.with_name(session_file.name + ".tmp")
        tmp.write_text(json.dumps(session, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(session_file))


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    tool = data.get("tool_name") or ""
    tool_input = data.get("tool_input") or {}
    cwd = Path(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT") or str(Path(__file__).resolve().parent.parent)

    if tool == "Bash":
        if is_own_script(tool_input.get("command") or "", plugin_root):
            decide("allow", "Recall: its own srs.py script.")
        return 0

    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not raw:
        return 0
    target = Path(raw)
    if not target.is_absolute():
        target = cwd / target
    target = Path(os.path.realpath(str(target)))
    vault = find_vault(target.parent) or find_vault(cwd.resolve())
    if vault is None:
        return 0
    try:
        rel = target.relative_to(vault.resolve()).as_posix()
    except ValueError:
        return 0
    parts = rel.split("/")
    if parts[0] == SRS_DIR:
        decide("deny", "Recall: files in .srs/ are written only by srs.py. Use the srs.py commands instead.")
        return 0
    session_file = vault / SRS_DIR / "session.json"
    if session_file.exists() and rel.lower().endswith(".md") and not any(p.startswith(".") for p in parts):
        record_touched(session_file, rel)
        decide("allow", "Recall: edit recorded for review; it is committed only after the user approves the git diff.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
