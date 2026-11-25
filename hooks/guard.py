#!/usr/bin/env python3
"""Recall's PreToolUse guard.

- Denies Claude's direct edits to a vault's .srs/ folder: only srs.py writes there.
- While a Recall command has a session open, lets Claude edit the vault's notes
  without a permission prompt and records each file, so the command can show
  exactly those files as a git diff and commit or discard them. The git diff is
  the review, so it works the same in every permission mode.
- Lets Bash run this plugin's own srs.py without a prompt, when the command is
  exactly that script and nothing else.
- Denies a page that would contain a `{CLAUDE: ...}` note: those notes are the
  user's instructions in the inbox and never go into a page. Only the inbox and
  the files of rules (CLAUDE.md, CLAUDE.local.md, AGENTS.md) may hold the text.

Anything else gets no opinion and follows the normal permission flow. Outside a
vault where Recall is set up, the guard does nothing.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

SRS_DIR = ".srs"
OPERATORS = set("();<>|&")
NOTE_RE = re.compile(r"\{\s*CLAUDE\b")
RULE_FILES = {"claude.md", "claude.local.md", "agents.md"}


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


def inbox_rel(vault: Path) -> str:
    try:
        config = json.loads((vault / SRS_DIR / "config.json").read_text(encoding="utf-8"))
        value = (config.get("paths") or {}).get("inbox") or "inbox.md"
    except (OSError, ValueError, AttributeError):
        value = "inbox.md"
    value = str(value).replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value.casefold()


def written_texts(tool_input: dict) -> list:
    texts = [tool_input.get("content"), tool_input.get("new_string"), tool_input.get("new_source")]
    texts += [e.get("new_string") for e in tool_input.get("edits") or [] if isinstance(e, dict)]
    return [t for t in texts if isinstance(t, str)]


def carries_note(vault: Path, rel: str, tool_input: dict) -> bool:
    """True when the write puts a `{CLAUDE: ...}` note into a page of the vault."""
    parts = rel.split("/")
    if not rel.lower().endswith(".md") or any(p.startswith(".") for p in parts):
        return False
    if rel.casefold() == inbox_rel(vault) or parts[-1].lower() in RULE_FILES:
        return False
    return any(NOTE_RE.search(t) for t in written_texts(tool_input))


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


def record_removed_notes(command: str, cwd: Path) -> None:
    """While a session is open, record the notes a plain `rm` deletes, so the session commits the removal.

    Only a bare `rm [flags] file...` counts: no shell operators, substitutions or newlines. Recording
    is harmless if the command fails, because the commit keeps only paths git sees as changed.
    """
    if not command or "`" in command or "$(" in command or "\n" in command.strip():
        return
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return
    if not tokens or any(t and set(t) <= OPERATORS for t in tokens) or os.path.basename(tokens[0]) != "rm":
        return
    for raw in tokens[1:]:
        if raw.startswith("-"):
            continue
        target = Path(raw)
        if not target.is_absolute():
            target = cwd / target
        target = Path(os.path.realpath(str(target)))
        vault = find_vault(target.parent) or find_vault(cwd.resolve())
        if vault is None:
            continue
        try:
            rel = target.relative_to(vault.resolve()).as_posix()
        except ValueError:
            continue
        parts = rel.split("/")
        session_file = vault / SRS_DIR / "session.json"
        if session_file.exists() and rel.lower().endswith(".md") and not any(p.startswith(".") for p in parts):
            record_touched(session_file, rel)


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
        command = tool_input.get("command") or ""
        if is_own_script(command, plugin_root):
            decide("allow", "Recall: its own srs.py script.")
        else:
            record_removed_notes(command, cwd)
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
    if carries_note(vault, rel, tool_input):
        decide("deny", "Recall: the text has a {CLAUDE: ...} note from the inbox. A note never goes into a page: "
                       "write the page without it.")
        return 0
    session_file = vault / SRS_DIR / "session.json"
    if session_file.exists() and rel.lower().endswith(".md") and not any(p.startswith(".") for p in parts):
        record_touched(session_file, rel)
        decide("allow", "Recall: edit recorded for review; it is committed only after the user approves the git diff.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
