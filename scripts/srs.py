#!/usr/bin/env python3
"""Recall engine.

The only writer of a vault's .srs/ folder and the only component that runs
git. Recall's commands call it for everything that must be exact: the review
schedule, the daily quiz list, index and page checks, inbox blocks, page
seals, and the git session around every operation.

Standard library only. Python 3.8 or later.
"""

from __future__ import annotations

import argparse
import copy
import csv
import datetime as dt
import fnmatch
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "0.16.1"
SRS_DIR = ".srs"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = PLUGIN_ROOT / "templates"
RULES_FILE = PLUGIN_ROOT / "rules.md"

SYSTEM_FILE_NAMES = {"claude.md", "claude.local.md", "agents.md"}
GITIGNORE_LINES = [".srs/session.json", ".srs/review/", ".obsidian/workspace*.json", ".trash/"]
SESSION_EXCLUDE = ":(exclude).srs/session.json"
LOCAL_SETTINGS = ".claude/settings.local.json"
LOCAL_SETTINGS_EXCLUDE = f":(exclude){LOCAL_SETTINGS}"
SUGGESTION_KEY = "promptSuggestionEnabled"
SUGGESTION_ENV = "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION"
GAP_CLOSE_DAYS = 3  # a pass closes a gap once this many days have passed since the previous answer
GAP_STUCK_FAILS = 3  # /maintenance reports a gap with this many fails
LOG_HEADER = ["day", "concept", "result", "phase", "interval"]
DESC_SEP = " — "  # space, em dash, space
SOURCE_RANK = {"integrate": 0, "promote": 1}
EXCLUSION_HINTS = ("template", "attachment", "asset", "excalidraw", "daily", "journal", "archive")
PHASES = ("new", "learning", "review", "relearning")
REVIEW_MODES = ("human", "agent")
REVIEW_DIR = "review"  # .srs/review/: state of the agent review, never committed
JUDGES = {"diff-judge": "VPER", "concept-judge": "ODI"}  # agent name -> the criteria letters it answers
JUDGE_PLUGIN = "recall"  # the plugin name that prefixes an agent type: recall:diff-judge
WIKILINK_RE = re.compile(r"!?\[\[([^\]\n]+?)\]\]")
ATTACHMENT_EXTS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "pdf", "mp3", "wav", "ogg", "mp4", "webm", "mov"}
BLOCK_ID_RE = re.compile(r"[0-9a-f]{8}(?:-\d+)?")

# Default parameters of the built-in scheduling algorithms (see docs/scheduling.md).
# A vault overrides them under scheduling.<algorithm> in .srs/config.json.

# Anki's default deck options for its classic scheduler (SM-2), adapted to pass and fail.
ANKI_DEFAULTS = {
    "graduating_interval": 1,  # days, after the first pass of a new concept
    "starting_ease": 2.5,  # interval multiplier a concept starts with
    "ease_penalty": 0.2,  # ease lost on each lapse
    "minimum_ease": 1.3,
    "hard_interval": 1.2,  # Anki's Hard multiplier; here it only sets the minimum growth
    "interval_modifier": 1.0,  # scales every passing interval
    "new_interval": 0.0,  # share of the interval kept after a lapse
    "minimum_interval": 1,  # days, the floor after a lapse
    "maximum_interval": 36500,  # days
    "fuzz": True,
    "leech_threshold": 8,  # lapses
}

# Recall's own rules: no ease, a fixed multiplier, and a lapse keeps a third of the interval.
THIRDS_DEFAULTS = {
    "multiplier": 2.5,
    "lapse_divisor": 3,
    "min_interval": 1,  # days
    "fuzz": 0.05,  # spread, either way
    "fuzz_min_interval": 3,  # days; shorter intervals are never spread
    "leech_threshold": 10,  # lapses, counting fails in relearning
}

PLACEMENTS = {
    "folders": "new pages go in the vault's existing folder whose topic fits best",
    "flat": "every new page goes in the new-pages folder, whatever the vault's folders",
}

DEFAULT_CONFIG = {
    "version": 1,
    "language": "English",
    "concept_language": "English",
    "paths": {"inbox": "inbox.md", "index": "index.md", "new_pages": "wiki", "folder_map": "folders.md"},
    "new_page_placement": "folders",
    "inbox_heading_level": 2,
    "integrate_review": "human",  # human: the user approves each diff; agent: judge agents review it
    # The one-time question and the reminders to try the agent review while the mode is human.
    # asked, asked_head and wait_days are state kept by `srs.py config`; the rest are limits.
    "integrate_review_prompt": {
        "asked": None,       # day of the last answer to the question or the reminder
        "asked_head": None,  # HEAD then, to count the blocks integrated since
        "wait_days": None,   # days to wait before the next reminder
        "first_days": 14,    # wait after an answer
        "max_days": 41,      # a "not now" doubles the wait, up to this
        "min_commits": 10,   # blocks integrated since the last answer before a reminder
    },
    "exclude": [],
    "scheduling": {
        "algorithm": "anki",
        "daily_limit": 20,
        "new_per_day": 5,
        "day_starts_at": 4,
        "mature_interval": 21,  # days; a review answered at this interval or longer is mature
        "anki": dict(ANKI_DEFAULTS),
        "thirds": dict(THIRDS_DEFAULTS),
    },
    "limits": {
        "page_max_chars": 6000,
        "description_max_chars": 60,
        "maintenance_batch": 20,
        "setup_batch": 20,
        "long_queue": 35,
    },
    "target_retention": [0.85, 0.9],
}

# /quiz: when more than PREPARE_ABOVE questions are left, a background fork drafts
# the questions from list item PREPARE_FROM on while the first ones are asked.
PREPARE_ABOVE = 10
PREPARE_FROM = 4


class SrsError(Exception):
    """Printed to stderr. Exit codes: 1 usage or failure, 2 refused, 3 interrupted session."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def norm_key(name: str) -> str:
    """Comparison key for concept names: case- and whitespace-insensitive."""
    return " ".join(name.split()).casefold()


def norm_rel(path: str) -> str:
    rel = str(path).replace("\\", "/").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    return rel.strip("/")


def parse_day(value):
    return dt.date.fromisoformat(value) if value else None


def days(n: int) -> dt.timedelta:
    return dt.timedelta(days=n)


def round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SrsError(f"{path} is not valid JSON ({exc}). Fix it, or restore it with git.")


def write_json(path: Path, data) -> None:
    """Stable formatting (sorted keys, 2 spaces) keeps git diffs small."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def read_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise SrsError(f"{path} is not UTF-8 text.")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def blob_hash(data: bytes) -> str:
    """The hash git gives this content as a blob (git hash-object)."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def short(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Vault and configuration
# ---------------------------------------------------------------------------


def find_vault(start: Path):
    """Walk up from start to the folder holding .srs/config.json."""
    cur = start.resolve()
    for folder in [cur, *cur.parents]:
        if (folder / SRS_DIR / "config.json").is_file():
            return folder
    return None


@dataclass
class Vault:
    root: Path
    config: dict
    today: dt.date

    @property
    def srs(self) -> Path:
        return self.root / SRS_DIR

    @property
    def inbox_rel(self) -> str:
        return norm_rel(self.config["paths"]["inbox"])

    @property
    def index_rel(self) -> str:
        return norm_rel(self.config["paths"]["index"])

    @property
    def folder_map_rel(self) -> str:
        return norm_rel(self.config["paths"]["folder_map"])

    @property
    def new_pages_rel(self) -> str:
        return norm_rel(self.config["paths"]["new_pages"])

    @property
    def placement(self) -> str:
        value = self.config["new_page_placement"]
        if value not in PLACEMENTS:
            raise SrsError(f"Unknown new_page_placement '{value}'. Edit .srs/config.json to use one of: "
                           + ", ".join(PLACEMENTS) + ".")
        return value

    @property
    def integrate_review(self) -> str:
        value = self.config["integrate_review"]
        if value not in REVIEW_MODES:
            raise SrsError(f"Unknown integrate_review '{value}'. Edit .srs/config.json to use one of: "
                           + ", ".join(REVIEW_MODES) + ".")
        return value

    @property
    def sched(self) -> dict:
        return self.config["scheduling"]

    @property
    def leech_at(self) -> int:
        """Lapses that flag a leech, from the configured algorithm."""
        return int(algorithm(self.sched)[1]["leech_threshold"])

    @property
    def limits(self) -> dict:
        return self.config["limits"]

    def rel(self, path: Path):
        """Vault-relative POSIX path, or None when path is outside the vault."""
        try:
            return path.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return None


def load_config(root: Path) -> dict:
    user = read_json(root / SRS_DIR / "config.json", {})
    return deep_merge(DEFAULT_CONFIG, user)


def compute_today(config: dict, override) -> dt.date:
    value = override or os.environ.get("RECALL_TODAY")
    if value:
        return dt.date.fromisoformat(value)
    hours = config["scheduling"]["day_starts_at"]
    return (dt.datetime.now() - dt.timedelta(hours=hours)).date()


def open_vault(args) -> Vault:
    start = Path(args.vault) if args.vault else Path.cwd()
    root = find_vault(start)
    if root is None:
        raise SrsError("Recall is not set up in this folder or any parent. Run /setup first.")
    config = load_config(root)
    return Vault(root=root, config=config, today=compute_today(config, args.today))


# ---------------------------------------------------------------------------
# Pages and exclusions
# ---------------------------------------------------------------------------


def list_markdown(root: Path) -> list:
    """Every .md file outside hidden folders, as sorted vault-relative paths."""
    found = []
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        base = Path(dirpath)
        for name in sorted(filenames):
            if name.lower().endswith(".md") and not name.startswith("."):
                found.append((base / name).relative_to(root).as_posix())
    return sorted(found)


def matches_pattern(rel: str, pattern: str) -> bool:
    """Exclusion patterns: 'folder/' (a folder), '*.excalidraw.md' (a name
    anywhere), 'Daily/*.md' (a path; '*' also crosses folders)."""
    pat = norm_rel(pattern).lower() + ("/" if pattern.strip().endswith("/") else "")
    rel = rel.lower()
    if not pat.strip("/"):
        return False
    if pat.endswith("/**"):
        pat = pat[:-2]
    if pat.endswith("/"):
        folder = pat.rstrip("/")
        return rel.startswith(folder + "/") or fnmatch.fnmatchcase(rel, folder + "/*")
    if "/" in pat:
        return fnmatch.fnmatchcase(rel, pat)
    parts = rel.split("/")
    return fnmatch.fnmatchcase(parts[-1], pat) or any(fnmatch.fnmatchcase(p, pat) for p in parts[:-1])


def is_system_file(v: Vault, rel: str) -> bool:
    return rel in (v.inbox_rel, v.index_rel, v.folder_map_rel) or rel.rsplit("/", 1)[-1].lower() in SYSTEM_FILE_NAMES


def is_excluded(v: Vault, rel: str) -> bool:
    return is_system_file(v, rel) or any(matches_pattern(rel, p) for p in v.config.get("exclude", []))


def content_pages(v: Vault) -> list:
    return [rel for rel in list_markdown(v.root) if not is_excluded(v, rel)]


def folder_outline(v: Vault, depth: int = 2) -> list:
    """Visible, non-excluded folders down to `depth` levels, as (path, pages) in
    tree order. Pages are content pages, counting the ones in deeper folders."""
    pages = content_pages(v)
    found = []

    def walk(base: str, level: int) -> None:
        folder = v.root / base if base else v.root
        for name in sorted(os.listdir(str(folder)), key=str.casefold):
            rel = f"{base}/{name}" if base else name
            if name.startswith(".") or not (folder / name).is_dir():
                continue
            if any(matches_pattern(rel + "/", p) for p in v.config.get("exclude", [])):
                continue
            found.append((rel, sum(1 for page in pages if page.startswith(rel + "/"))))
            if level < depth:
                walk(rel, level + 1)

    walk("", 1)
    return found


TABLE_SEPARATOR = re.compile(r"^:?-+:?$")


def folder_map_rows(text: str) -> list:
    """Folder names from the first column of every table in the folder map,
    leaving out each table's header and separator rows."""
    names, in_table = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            in_table = False
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not in_table:  # the header row
            in_table = True
            continue
        if all(TABLE_SEPARATOR.match(c) for c in cells if c):
            continue
        name = cells[0].strip("*_` ").rstrip("/").strip()
        if name:
            names.append(name)
    return names


def folder_map_checks(v: Vault) -> dict:
    """With folders placement: top-level folders that hold pages but have no row in
    the folder map, and rows that name no top-level folder. Without a map file,
    every folder with pages is reported as missing."""
    found = {"unmapped_folders": [], "map_rows_without_folder": []}
    if v.placement != "folders":
        return found
    top = dict(folder_outline(v, depth=1))
    path = v.root / v.folder_map_rel
    rows = folder_map_rows(read_text(path)) if path.is_file() else []
    mapped = {r.casefold() for r in rows}
    found["unmapped_folders"] = [rel for rel, n in top.items() if n and rel.casefold() not in mapped]
    existing = {rel.casefold() for rel in top}
    found["map_rows_without_folder"] = [r for r in rows if r.casefold() not in existing]
    return found


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

HEADER_RE = re.compile(r"^##[ \t]+\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\][ \t]*$")
ITEM_RE = re.compile(r"^[ \t]*[-*+][ \t]+(.+?)[ \t]*$")
HEADING_RE = re.compile(r"^#{1,6}[ \t]")
FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


@dataclass
class IndexConcept:
    name: str
    desc: object
    line: int
    page: "IndexPage"


@dataclass
class IndexPage:
    link: str
    line: int
    header: str
    concepts: list = field(default_factory=list)
    path: object = None
    status: str = "unresolved"  # ok | missing | ambiguous | excluded
    candidates: list = field(default_factory=list)

    def entry_text(self) -> str:
        lines = [self.header.strip()]
        for c in self.concepts:
            lines.append(f"- {c.name}" + (f"{DESC_SEP}{c.desc}" if c.desc else ""))
        return "\n".join(lines)

    def entry_hash(self) -> str:
        return hashlib.sha1(self.entry_text().encode("utf-8")).hexdigest()[:12]


@dataclass
class Index:
    pages: list
    problems: list  # (line number, message)

    def concepts(self):
        for page in self.pages:
            yield from page.concepts

    def by_key(self) -> dict:
        found = defaultdict(list)
        for c in self.concepts():
            found[norm_key(c.name)].append(c)
        return found

    def page_by_path(self, rel: str):
        for page in self.pages:
            if page.path == rel:
                return page
        return None


class FenceTracker:
    """Tells whether a line is inside a fenced code block."""

    def __init__(self):
        self.marker = None

    def step(self, line: str) -> bool:
        """Feed one line; return True when the line is fence or fenced content."""
        m = FENCE_RE.match(line)
        if self.marker is None:
            if m:
                self.marker = m.group(1)
                return True
            return False
        if m and m.group(1)[0] == self.marker[0] and len(m.group(1)) >= len(self.marker):
            self.marker = None
        return True


def parse_index(text: str) -> Index:
    pages, problems = [], []
    current = None
    fence = FenceTracker()
    for no, raw in enumerate(text.splitlines(), start=1):
        line = raw.lstrip("﻿") if no == 1 else raw
        if fence.step(line):
            continue
        if not line.strip():
            continue
        m = HEADER_RE.match(line)
        if m:
            current = IndexPage(link=m.group(1).strip(), line=no, header=line)
            pages.append(current)
            continue
        if HEADING_RE.match(line):
            current = None
            continue
        m = ITEM_RE.match(line)
        if m:
            if current is None:
                problems.append((no, f"concept outside a page section: {short(line)}"))
                continue
            body = m.group(1)
            if DESC_SEP in body:
                name, desc = body.split(DESC_SEP, 1)
                name, desc = name.strip(), desc.strip() or None
            else:
                name, desc = body.strip(), None
            current.concepts.append(IndexConcept(name=name, desc=desc, line=no, page=current))
            continue
        if current is not None and not line.lstrip().startswith(("<!--", ">")):
            problems.append((no, f"unrecognised line inside [[{current.link}]]: {short(line)}"))
    return Index(pages=pages, problems=problems)


def link_tables(v: Vault):
    """The two lookups Obsidian uses to resolve a link: by file name, and by path without .md."""
    by_stem = defaultdict(list)
    by_path = {}
    for rel in list_markdown(v.root):
        by_stem[Path(rel).stem.casefold()].append(rel)
        by_path[rel[:-3].casefold()] = rel
    return by_stem, by_path


def link_candidates(tables, link: str) -> list:
    """The files a link can mean: by file name when it has no slash, by path (or path suffix) otherwise."""
    by_stem, by_path = tables
    link = link.strip().replace("\\", "/")
    if link.lower().endswith(".md"):
        link = link[:-3]
    link = link.strip("/")
    if "/" in link:
        exact = by_path.get(link.casefold())
        if exact:
            cands = [exact]
        else:
            suffix = "/" + link.casefold()
            cands = [rel for key, rel in by_path.items() if key.endswith(suffix)]
    else:
        cands = list(by_stem.get(link.casefold(), []))
    return sorted(cands)


def resolve_links(v: Vault, idx: Index) -> None:
    """Resolve [[links]] the way Obsidian does: by file name when unique, by path otherwise."""
    tables = link_tables(v)
    for page in idx.pages:
        cands = link_candidates(tables, page.link)
        page.candidates = cands
        if len(cands) == 1:
            page.path = cands[0]
            page.status = "excluded" if is_excluded(v, page.path) else "ok"
        else:
            page.path = None
            page.status = "missing" if not cands else "ambiguous"


def load_index(v: Vault):
    path = v.root / v.index_rel
    text = read_text(path) if path.exists() else ""
    idx = parse_index(text)
    resolve_links(v, idx)
    return idx, text


# ---------------------------------------------------------------------------
# State, log and seals
# ---------------------------------------------------------------------------


def empty_state() -> dict:
    return {"version": 1, "next_order": 1, "concepts": {}, "flags": {"thin": {}, "misplaced": {}}}


def load_state(v: Vault) -> dict:
    state = read_json(v.srs / "state.json", empty_state())
    state.setdefault("concepts", {})
    state.setdefault("next_order", 1)
    flags = state.setdefault("flags", {})
    flags.setdefault("thin", {})
    flags.setdefault("misplaced", {})
    return state


def save_state(v: Vault, state: dict) -> None:
    write_json(v.srs / "state.json", state)
    touch(v, f"{SRS_DIR}/state.json")


def find_state_name(state: dict, name: str):
    if name in state["concepts"]:
        return name
    key = norm_key(name)
    for existing in state["concepts"]:
        if norm_key(existing) == key:
            return existing
    return None


def new_record(source: str, today: dt.date, order: int) -> dict:
    return {
        "phase": "new",
        "source": source,
        "queued": today.isoformat(),
        "order": order,
        "due": None,
        "interval": 0,
        "pending": None,
        "lapses": 0,
        "reps": 0,
        "last": None,
        "introduced": None,
        "focus": None,
        "questions": [],
    }


def append_log(v: Vault, row: list) -> None:
    path = v.srs / "log.csv"
    exists = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if exists:
        # Logs from before 0.5.0 have no interval column: widen the header; old rows read it as empty.
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        header = ",".join(LOG_HEADER)
        if lines and lines[0].strip() != header:
            path.write_text(header + "\n" + "".join(lines[1:]), encoding="utf-8")
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        if not exists:
            writer.writerow(LOG_HEADER)
        writer.writerow(row)
    touch(v, f"{SRS_DIR}/log.csv")


def read_log(v: Vault) -> list:
    path = v.srs / "log.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def load_verified(v: Vault) -> dict:
    return read_json(v.srs / "verified.json", {})


def seal_ok(v: Vault, verified: dict, page: IndexPage) -> bool:
    record = verified.get(page.path or "")
    if not record or page.status != "ok":
        return False
    data = (v.root / page.path).read_bytes()
    return record.get("blob") == blob_hash(data) and record.get("entry") == page.entry_hash()


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def unit_random(name: str, day: dt.date) -> float:
    """A number in [0, 1) derived from the concept name and the day: fuzz that
    gives the same date for the same input."""
    digest = hashlib.sha1(f"{name}|{day.isoformat()}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") / 2 ** 32


def fuzzed(interval: int, name: str, day: dt.date, params: dict) -> int:
    """thirds: spread an interval by up to +/- the fuzz share, and at least +/- 1 day,
    deterministically. Without the 1-day floor, 5% rounds to nothing below 10 days and
    concepts learned together would stay due on the same days."""
    if interval < params["fuzz_min_interval"] or params["fuzz"] <= 0:
        return interval
    r = unit_random(name, day) * 2 - 1
    spread = max(1.0, interval * params["fuzz"])
    return max(params["min_interval"], interval + round_half_up(spread * r))


ANKI_FUZZ_RANGES = ((2.5, 7.0, 0.15), (7.0, 20.0, 0.10), (20.0, math.inf, 0.05))


def anki_interval(interval: float, minimum: int, maximum: int, name: str, day: dt.date, fuzz: bool) -> int:
    """anki: round an interval into [minimum, maximum], with Anki's fuzz when enabled.

    As in Anki, intervals under 2.5 days are never fuzzed; longer ones may move by
    1 day plus 15% of the part between 2.5 and 7 days, 10% of the part between 7 and
    20 days, and 5% of the part above 20 days."""
    minimum = max(1, min(int(minimum), int(maximum)))
    interval = min(max(float(interval), minimum), maximum)
    if not fuzz or interval < 2.5:
        return min(max(round_half_up(interval), minimum), maximum)
    delta = 1.0 + sum(share * max(0.0, min(interval, end) - start) for start, end, share in ANKI_FUZZ_RANGES)
    lower = min(max(round_half_up(interval - delta), minimum), maximum)
    upper = min(max(round_half_up(interval + delta), minimum), maximum)
    if upper == lower and 2 < upper < maximum:
        upper = lower + 1
    return lower + int(unit_random(name, day) * (upper - lower + 1))


def schedule_anki(rec: dict, passed: bool, today: dt.date, p: dict, name: str) -> None:
    """anki: Anki's classic scheduler (SM-2) with its default options, adapted to
    pass (Anki's Good) and fail (Again). Anki's learning and relearning steps last
    minutes; here a step is the next day's quiz."""
    tomorrow = (today + days(1)).isoformat()
    maximum = int(p["maximum_interval"])
    floor = max(1, int(p["minimum_interval"]))
    ease = float(rec.get("ease") or p["starting_ease"])
    phase = rec["phase"]
    if phase in ("new", "learning"):
        if passed:  # graduates
            interval = anki_interval(p["graduating_interval"], 1, maximum, name, today, p["fuzz"])
            rec.update(phase="review", interval=interval, pending=None, ease=round(ease, 2),
                       due=(today + days(interval)).isoformat())
        else:
            rec.update(phase="learning", interval=0, pending=None, due=tomorrow)
    elif phase == "review":
        current = max(1, int(rec.get("interval") or 1))
        if passed:
            due = parse_day(rec.get("due")) or today
            late = max(0, (today - due).days)
            modifier = float(p["interval_modifier"])
            hard = float(p["hard_interval"])
            if hard > 1:  # as in Anki, a pass grows at least one day more than Hard would
                minimum = min(max(round_half_up(current * hard * modifier), current + 1), maximum) + 1
            else:
                minimum = current + 1
            good = (current + late / 2) * ease * modifier
            interval = anki_interval(good, min(minimum, maximum), maximum, name, today, p["fuzz"])
            rec.update(interval=interval, due=(today + days(interval)).isoformat())
        else:  # lapse
            rec.update(
                phase="relearning",
                ease=round(max(float(p["minimum_ease"]), ease - float(p["ease_penalty"])), 2),
                pending=min(max(floor, round_half_up(current * float(p["new_interval"]))), maximum),
                due=tomorrow,
                lapses=int(rec.get("lapses") or 0) + 1,
            )
    else:  # relearning
        if passed:
            pending = max(floor, int(rec.get("pending") or floor))
            interval = anki_interval(pending, floor, maximum, name, today, p["fuzz"])
            rec.update(phase="review", interval=interval, pending=None, due=(today + days(interval)).isoformat())
        else:  # repeat the step: no new lapse, no ease change
            rec.update(due=tomorrow)


def schedule_thirds(rec: dict, passed: bool, today: dt.date, p: dict, name: str) -> None:
    """thirds: no ease; a pass multiplies the interval, and a lapse keeps a third
    of it, then a third of that at each new fail, down to the minimum."""
    mult, div, low = float(p["multiplier"]), float(p["lapse_divisor"]), int(p["min_interval"])
    tomorrow = (today + days(1)).isoformat()
    phase = rec["phase"]
    if phase in ("new", "learning"):
        if passed:  # graduates; back at the next review
            rec.update(phase="review", interval=1, pending=None, due=tomorrow)
        else:
            rec.update(phase="learning", interval=0, pending=None, due=tomorrow)
    elif phase == "review":
        interval = max(low, int(rec.get("interval") or low))
        if passed:
            last = parse_day(rec.get("last")) or today
            elapsed = (today - last).days
            base = interval if elapsed <= interval else (interval + elapsed) / 2
            new = max(interval + 1, round_half_up(base * mult))
            new = max(interval + 1, fuzzed(new, name, today, p))
            rec.update(interval=new, due=(today + days(new)).isoformat())
        else:
            rec.update(
                phase="relearning",
                pending=max(low, round_half_up(interval / div)),
                due=tomorrow,
                lapses=int(rec.get("lapses") or 0) + 1,
            )
    else:  # relearning
        pending = max(low, int(rec.get("pending") or low))
        if passed:
            new = fuzzed(pending, name, today, p)
            rec.update(phase="review", interval=new, pending=None, due=(today + days(new)).isoformat())
        else:
            rec.update(
                pending=max(low, round_half_up(pending / div)),
                due=tomorrow,
                lapses=int(rec.get("lapses") or 0) + 1,
            )


@dataclass(frozen=True)
class Algorithm:
    summary: str  # one line, shown by /setup
    defaults: dict
    schedule: object  # function(rec, passed, today, params, name): updates the record in place
    growth_key: str  # the parameter to adjust when retention is off target


# To add an algorithm: write a function like schedule_anki, give it default
# parameters, and register it here. Its name is then valid in scheduling.algorithm.
ALGORITHMS = {
    "anki": Algorithm(
        "Anki's classic scheduler (SM-2) with Anki's default options: each concept has an ease that "
        "drops on every lapse, and a lapse restarts the interval at 1 day.",
        ANKI_DEFAULTS, schedule_anki, "interval_modifier"),
    "thirds": Algorithm(
        "Recall's own rules: no ease, a pass multiplies the interval by 2.5, and a lapse keeps a third of it.",
        THIRDS_DEFAULTS, schedule_thirds, "multiplier"),
}


def algorithm(sched: dict):
    """The configured algorithm: its name, its parameters (defaults filled in) and its entry."""
    name = str(sched.get("algorithm") or "anki").strip().lower()
    if name not in ALGORITHMS:
        raise SrsError(f"Unknown scheduling algorithm '{sched.get('algorithm')}' in .srs/config.json. "
                       f"Use one of: {', '.join(ALGORITHMS)}.")
    entry = ALGORITHMS[name]
    params = dict(entry.defaults)
    params.update(sched.get(name) or {})
    return name, params, entry


def apply_answer(rec: dict, passed: bool, today: dt.date, sched: dict, name: str) -> dict:
    """Apply one answer with the configured algorithm. Returns the updated record."""
    rec = dict(rec)
    if rec.get("phase") not in PHASES:
        raise SrsError(f"Unknown phase '{rec.get('phase')}' for {name}.")
    _, params, entry = algorithm(sched)
    held = rec.pop("hold", False)
    if held and passed and rec["phase"] == "review":
        # The new content was remembered: keep the interval, without growth.
        interval = max(1, int(rec.get("interval") or 1))
        rec["due"] = (today + days(interval)).isoformat()
    else:  # a fail on hold is a normal lapse
        entry.schedule(rec, passed, today, params, name)
    rec["reps"] = int(rec.get("reps") or 0) + 1
    rec["introduced"] = rec.get("introduced") or today.isoformat()
    rec["last"] = today.isoformat()
    rec["focus"] = None
    return rec


def apply_update(rec: dict, today: dt.date, focus: str) -> dict:
    """New content for a scheduled concept: the next question targets it. A concept in
    review comes back tomorrow on hold: a pass keeps its interval, a fail is a normal lapse."""
    rec = dict(rec)
    if rec["phase"] == "review":
        rec.update(hold=True, due=(today + days(1)).isoformat())
    rec["focus"] = focus
    return rec


def is_due(rec: dict, today: dt.date) -> bool:
    if rec["phase"] == "new":
        queued = parse_day(rec.get("queued"))
        return queued is not None and queued < today
    due = parse_day(rec.get("due"))
    return due is not None and due <= today


def index_neighbors(idx: Index) -> dict:
    """For each concept (by norm_key), the concepts on the lines just above and below
    it in its page's section of the index: they are usually written side by side and
    defined in terms of each other."""
    found = defaultdict(set)
    for page in idx.pages:
        keys = [norm_key(c.name) for c in page.concepts]
        for a, b in zip(keys, keys[1:]):
            if a != b:
                found[a].add(b)
                found[b].add(a)
    return dict(found)


def vault_neighbors(v) -> dict:
    """index_neighbors for the vault's index, read without resolving its links. An
    unreadable index gives no neighbors: the quiz goes on, and /maintenance reports it."""
    path = v.root / v.index_rel
    try:
        text = read_text(path) if path.exists() else ""
    except (SrsError, OSError):
        text = ""
    return index_neighbors(parse_index(text))


def daily_list(state: dict, sched: dict, today: dt.date, neighbors=None):
    """Today's quiz list and a summary. Learning, relearning and updated concepts
    first, then reviews (most overdue first, shorter interval first), then new
    concepts in the slots left, up to the daily limit.

    A new concept waits for another day when a neighbor in the index (see
    index_neighbors) is already in today's list or was answered today, so that two
    concepts defined side by side don't start, and stay, on the same days. A waiting
    concept keeps its place at the front of the queue; reviews never wait."""
    neighbors = neighbors or {}
    concepts = state["concepts"]
    t = today.isoformat()
    done = [n for n, r in concepts.items() if r.get("last") == t]
    new_done = [n for n, r in concepts.items() if r.get("introduced") == t]
    capacity = max(0, sched["daily_limit"] - len(done))

    def pending(names):
        return [n for n in names if n not in done and is_due(concepts[n], today)]

    def first(rec):
        return rec["phase"] in ("learning", "relearning") or bool(rec.get("hold"))

    learning = sorted(
        pending([n for n, r in concepts.items() if first(r)]),
        key=lambda n: (concepts[n]["due"], norm_key(n)),
    )
    reviews = sorted(
        pending([n for n, r in concepts.items() if r["phase"] == "review" and not first(r)]),
        key=lambda n: (concepts[n]["due"], concepts[n].get("interval") or 0, norm_key(n)),
    )
    due_old = learning + reviews
    items = due_old[:capacity]
    carried = len(due_old) - len(items)
    waiting_new = sorted(
        pending([n for n, r in concepts.items() if r["phase"] == "new"]),
        key=lambda n: (SOURCE_RANK.get(concepts[n].get("source"), 9), concepts[n]["queued"], concepts[n].get("order", 0)),
    )
    slots = 0
    if carried == 0:
        slots = max(0, min(sched["new_per_day"] - len(new_done), capacity - len(items)))
    taken = {norm_key(n) for n in done + items}
    for n in waiting_new:
        if slots <= 0:
            break
        key = norm_key(n)
        if neighbors.get(key, set()) & taken:
            continue
        items.append(n)
        taken.add(key)
        slots -= 1
    started = sum(1 for n in items if concepts[n]["phase"] == "new")
    info = {
        "done": len(done),
        "capacity": capacity,
        "carried": carried,
        "learning": len([n for n in items if first(concepts[n])]),
        "reviews": len([n for n in items if concepts[n]["phase"] == "review" and not first(concepts[n])]),
        "new": len([n for n in items if concepts[n]["phase"] == "new"]),
        "new_waiting": len(waiting_new) - started,
    }
    return items, info


def next_due(state: dict, sched: dict, today: dt.date, neighbors=None):
    """The next study day whose list really has questions, and how many. (None, 0) when none
    ever will at this pace: a queue of new concepts with new_per_day at 0 is never asked."""
    candidates = {today + days(1)}
    for rec in state["concepts"].values():
        if rec["phase"] == "new":
            queued = parse_day(rec.get("queued"))
            day = queued + days(1) if queued else None
        else:
            day = parse_day(rec.get("due"))
        if day and day > today:
            candidates.add(day)
    for day in sorted(candidates):
        items, _ = daily_list(state, sched, day, neighbors)
        if items:
            return day, len(items)
    return None, 0


def new_pace(state: dict, sched: dict, today: dt.date) -> float:
    """New concepts started per study day over the last 14 days."""
    start = today - days(13)
    intro = [parse_day(r["introduced"]) for r in state["concepts"].values() if r.get("introduced")]
    recent = [d for d in intro if start <= d <= today]
    if not recent:
        return float(sched["new_per_day"])
    span = min(14, (today - min(recent)).days + 1)
    return max(len(recent) / span, 0.1)


def queue_summary(state: dict, v: Vault) -> str:
    queue = sum(1 for r in state["concepts"].values() if r["phase"] == "new")
    if queue == 0:
        return "New-concept queue: empty."
    pace = new_pace(state, v.sched, v.today)
    days_needed = math.ceil(queue / pace)
    long_flag = " (long: suggest fewer new concepts)" if queue > v.limits["long_queue"] else ""
    return (f"New-concept queue: {queue} waiting, about {plural(days_needed, 'day')} at the current pace "
            f"of {pace:.1f} per day{long_flag}.")


def describe_record(rec: dict) -> str:
    phase = rec["phase"]
    if phase == "new":
        return f"new (queued {rec.get('queued')}, from /{rec.get('source')})"
    if phase == "learning":
        return "learning"
    if phase == "review":
        return f"review, interval {plural(rec.get('interval') or 0, 'day')}, lapses {rec.get('lapses', 0)}"
    return f"relearning, interval after a pass {plural(rec.get('pending') or 0, 'day')}, lapses {rec.get('lapses', 0)}"


def describe_answer(name: str, before: str, rec: dict, today: dt.date, leech_at: int) -> str:
    due = parse_day(rec["due"])
    wait = (due - today).days if due else 0
    when = "tomorrow" if wait == 1 else f"in {plural(wait, 'day')} ({rec['due']})"
    if rec["phase"] == "relearning":
        text = f"{name}: back {when}; interval after a pass: {plural(rec['pending'], 'day')}."
    elif rec["phase"] == "learning":
        text = f"{name}: back {when}."
    else:
        text = f"{name}: next review {when}."
    if rec.get("lapses", 0) >= leech_at and rec["phase"] == "relearning":
        text += f" LEECH: {rec['lapses']} lapses."
    return text


# ---------------------------------------------------------------------------
# Inbox blocks
# ---------------------------------------------------------------------------


@dataclass
class Block:
    id: str
    title: str
    start: int  # first line index
    end: int  # one past the last line
    text: str


def parse_blocks(text: str, level: int):
    """Blocks open at a heading of exactly `level` and run to the next heading
    of that level or higher. Headings inside code fences don't count."""
    lines = text.splitlines(keepends=True)
    opener = re.compile(r"^#{%d}[ \t]+(.*?)[ \t#]*$" % level)
    stopper = re.compile(r"^#{1,%d}[ \t]" % level)
    fence = FenceTracker()
    marks = []
    for i, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        if i == 0:
            line = line.lstrip("﻿")
        if fence.step(line):
            continue
        if stopper.match(line):
            m = opener.match(line)
            marks.append((i, m.group(1).strip() if m else None))
    blocks, seen = [], defaultdict(int)
    for n, (start, title) in enumerate(marks):
        if title is None:
            continue
        end = marks[n + 1][0] if n + 1 < len(marks) else len(lines)
        body = "".join(lines[start:end])
        digest = hashlib.sha1(body.replace("\r\n", "\n").rstrip().encode("utf-8")).hexdigest()[:8]
        seen[digest] += 1
        block_id = digest if seen[digest] == 1 else f"{digest}-{seen[digest]}"
        blocks.append(Block(id=block_id, title=title, start=start, end=end, text=body))
    return blocks, lines


def heading_counts(text: str) -> dict:
    counts = defaultdict(int)
    fence = FenceTracker()
    for raw in text.splitlines():
        if fence.step(raw):
            continue
        m = re.match(r"^(#{1,6})[ \t]", raw)
        if m:
            counts[len(m.group(1))] += 1
    return dict(counts)


def block_source(v: Vault, file_arg):
    rel = norm_rel(file_arg) if file_arg else v.inbox_rel
    path = v.root / rel
    if v.rel(path) is None:
        raise SrsError(f"{file_arg} is outside the vault.")
    return rel, path


# ---------------------------------------------------------------------------
# Git session
# ---------------------------------------------------------------------------


def git(args: list, cwd: Path, check: bool = True):
    cmd = ["git", "-c", "core.quotepath=off", "-c", "color.ui=never"] + list(args)
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise SrsError("git is not installed or not on PATH. Recall needs git.")
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise SrsError(f"git {' '.join(args)} failed: {detail}")
    return proc


def head_sha(v: Vault) -> str:
    return git(["rev-parse", "HEAD"], v.root).stdout.strip()


def git_toplevel(path: Path):
    proc = git(["rev-parse", "--show-toplevel"], path, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def git_has_head(path: Path) -> bool:
    return git(["rev-parse", "--verify", "--quiet", "HEAD"], path, check=False).returncode == 0


def git_identity_problem(path: Path):
    for var in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        if git(["var", var], path, check=False).returncode != 0:
            return ("git doesn't know who you are. Run once:\n"
                    '  git config --global user.name "Your Name"\n'
                    '  git config --global user.email "you@example.com"')
    return None


class Repo:
    """Git access scoped to the vault (which may be a subfolder of the repository)."""

    def __init__(self, vault_root: Path):
        top = git_toplevel(vault_root)
        if top is None:
            raise SrsError("This vault is not a git repository. Recall is git-native; run /setup.")
        self.root = vault_root
        rel = os.path.relpath(str(vault_root.resolve()), str(Path(top).resolve()))
        self.prefix = "" if rel == "." else norm_rel(rel) + "/"

    def to_vault(self, repo_path: str):
        if not self.prefix:
            return repo_path
        return repo_path[len(self.prefix):] if repo_path.startswith(self.prefix) else None

    def status(self, pathspecs=None) -> dict:
        """{vault-relative path: porcelain XY} for changed paths."""
        args = ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--"]
        args += pathspecs or [".", SESSION_EXCLUDE, LOCAL_SETTINGS_EXCLUDE]
        entries = git(args, self.root).stdout.split("\0")
        changed, i = {}, 0
        while i < len(entries):
            item = entries[i]
            i += 1
            if len(item) < 4:
                continue
            xy, path = item[:2], item[3:]
            if xy[0] in "RC":
                i += 1  # the original path follows
            vault_path = self.to_vault(path)
            if vault_path is not None:
                changed[vault_path] = xy
        return changed

    def in_head(self, rel: str) -> bool:
        return git(["cat-file", "-e", f"HEAD:./{rel}"], self.root, check=False).returncode == 0

    def commit(self, paths: list, message: str) -> str:
        git(["add", "-A", "--"] + paths, self.root)
        git(["commit", "-q", "-m", message, "--"] + paths, self.root)
        return git(["rev-parse", "--short", "HEAD"], self.root).stdout.strip()

    def snapshot(self, message: str = "vault: manual edits") -> int:
        changed = self.status()
        if not changed:
            return 0
        excludes = [SESSION_EXCLUDE]
        # git add fails on an exclude that names an existing ignored file, and an ignored file needs none.
        if git(["check-ignore", "-q", "--", LOCAL_SETTINGS], self.root, check=False).returncode != 0:
            excludes.append(LOCAL_SETTINGS_EXCLUDE)
        git(["add", "-A", "--", "."] + excludes, self.root)
        git(["commit", "-q", "-m", message], self.root)
        return len(changed)


def require_repo(v: Vault) -> Repo:
    repo = Repo(v.root)
    if not git_has_head(v.root):
        raise SrsError("The vault's git repository has no commits yet. Run /setup.")
    problem = git_identity_problem(v.root)
    if problem:
        raise SrsError(problem, 2)
    return repo


def session_path(v: Vault) -> Path:
    return v.srs / "session.json"


def load_session(v: Vault):
    return read_json(session_path(v), None)


def save_session(v: Vault, session: dict) -> None:
    write_json(session_path(v), session)


def require_session(v: Vault) -> dict:
    session = load_session(v)
    if session is None:
        raise SrsError("No session is open. Start one with srs.py begin <command>.")
    return session


def touch(v: Vault, rel: str) -> None:
    """Record a file written during the open session (no-op without one)."""
    session = load_session(v)
    if session is None:
        return
    rel = norm_rel(rel)
    touched = session.setdefault("touched", [])
    if rel not in touched:
        touched.append(rel)
        save_session(v, session)


def pending_paths(repo: Repo, session: dict) -> list:
    changed = repo.status()
    return [p for p in session.get("touched", []) if p in changed]


def unrecorded_edits(repo: Repo, session: dict) -> list:
    """Notes changed during the session that the guard did not record: written outside Edit and Write."""
    touched = set(session.get("touched", []))
    return sorted(p for p in repo.status()
                  if p not in touched and not any(part.startswith(".") for part in p.split("/")))


def unrecorded_warning(repo: Repo, session: dict) -> list:
    paths = unrecorded_edits(repo, session)
    if not paths:
        return []
    return [f"Warning: changed but not recorded by this session, so left out of the diff: {', '.join(paths)}.",
            "If you wrote them, write them again with Edit or Write: only those tools are recorded. "
            "If the user edited them in Obsidian, leave them: they are committed later as manual edits."]


def interrupted_report(repo: Repo, session: dict) -> str:
    pending = pending_paths(repo, session)
    lines = [f"INTERRUPTED SESSION: /{session.get('command')} started {session.get('started')} was never closed."]
    if pending:
        lines.append("Files it left uncommitted:")
        lines += [f"  {p}" for p in pending]
    else:
        lines.append("It left nothing uncommitted.")
    command = session.get("command")
    lines.append(
        "Ask the user whether to keep or discard them. Keep: srs.py commit -m "
        f"\"{command}: recovered session\", then srs.py end. Discard: srs.py abort."
    )
    return "\n".join(lines)


def ensure_gitignore(root: Path) -> bool:
    path = root / ".gitignore"
    existing = read_text(path).splitlines() if path.exists() else []
    missing = [line for line in GITIGNORE_LINES if line not in existing]
    if not missing:
        return False
    text = "\n".join(existing).rstrip("\n")
    block = ("\n\n" if text else "") + "# Recall\n" + "\n".join(missing) + "\n"
    write_text(path, text + block)
    return True


def read_local_settings(path: Path) -> dict:
    """The vault's local settings; raises ValueError or OSError if they can't be used."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


def write_local_settings(path: Path, data: dict) -> None:
    """Keeps the order of the user's keys, unlike write_json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def hide_suggestions(v: Vault) -> list:
    """Turn prompt suggestions off in this vault: they can show a quiz answer in the input line.

    Claude Code reads the setting only when it starts, so a change made here hides them from the
    next session on, not in the one running the quiz. The setting stays off afterwards: putting it
    back at the end of the quiz would turn suggestions on again before any quiz could benefit.
    Returns notes for the user.
    """
    notes = []
    if os.environ.get(SUGGESTION_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        notes.append(
            "Tell the user, before the first question: suggested replies in the input line can show the "
            f"answer to each question. The environment variable {SUGGESTION_ENV} keeps them on and overrides "
            "Recall's setting, so they should turn it off where it is defined."
        )
    path = v.root / LOCAL_SETTINGS
    try:
        data = read_local_settings(path)
        if data.get(SUGGESTION_KEY) is False:
            return notes
        data[SUGGESTION_KEY] = False
        write_local_settings(path, data)
    except (OSError, ValueError) as exc:
        notes.append(
            "Tell the user, before the first question: suggested replies in the input line can show the "
            f"answer to each question, and Recall could not turn them off ({LOCAL_SETTINGS}: {exc}). "
            "They should turn off Prompt suggestions in /config."
        )
        return notes
    notes.append(
        "Tell the user, before the first question: suggested replies in the input line can show the "
        f"answer to each question. Recall has turned them off in this vault ({LOCAL_SETTINGS}), but Claude "
        "Code reads that setting only when it starts, so they stay on in this session. To hide them now, "
        "turn off Prompt suggestions in /config (that changes it for every project), or restart Claude "
        "Code in the vault and run /quiz again. From the next session on they are off in this vault."
    )
    return notes


def session_minutes(session: dict):
    """Whole minutes since the session opened, or None when the start time can't be read."""
    try:
        started = dt.datetime.fromisoformat(session["started"])
    except (KeyError, TypeError, ValueError):
        return None
    return max(0, round((dt.datetime.now() - started).total_seconds() / 60))


# ---------------------------------------------------------------------------
# Checks (the deterministic half of /maintenance)
# ---------------------------------------------------------------------------


def index_checks(v: Vault, idx: Index, lines=None) -> dict:
    """The checks that look only at the index: names used twice, long descriptions,
    links that don't resolve and lines that can't be read. With `lines` (a set of
    index line numbers), report only what sits on those lines; a repeated name is
    reported when any of its entries is on them."""
    limit = v.limits["description_max_chars"]

    def wanted(no: int) -> bool:
        return lines is None or no in lines

    found = {"duplicates": [], "long_descriptions": [], "link_problems": [],
             "index_problems": [p for p in idx.problems if wanted(p[0])]}
    for page in idx.pages:
        if page.status != "ok" and wanted(page.line):
            found["link_problems"].append(page)
        for c in page.concepts:
            if c.desc and len(c.desc) > limit and wanted(c.line):
                found["long_descriptions"].append((c.name, len(c.desc)))
    for key, entries in sorted(idx.by_key().items()):
        if len(entries) > 1 and any(wanted(e.line) for e in entries):
            found["duplicates"].append((entries[0].name, [f"[[{e.page.link}]]" for e in entries]))
    return found


def stale_seals(v: Vault, verified: dict) -> list:
    """Seals whose file is gone or is no longer a content page (excluded by the
    config, the inbox, the index or a hidden folder)."""
    pages = set(content_pages(v))
    return sorted(rel for rel in verified if rel not in pages)


def run_checks(v: Vault) -> dict:
    idx, _ = load_index(v)
    state = load_state(v)
    verified = load_verified(v)
    limits = v.limits
    pages = content_pages(v)
    indexed = {p.path for p in idx.pages if p.path}
    report = {
        "over_size": [],
        "orphans": [],
        "unindexed": [p for p in pages if p not in indexed],
        "unverified": [],
        "stale_seals": stale_seals(v, verified),
        "thin": sorted(state["flags"]["thin"].items()),
        "misplaced": sorted((name, f.get("page", ""), f.get("to", ""), f.get("day", ""))
                            for name, f in state["flags"]["misplaced"].items()),
        "leeches": [],
        "stuck_gaps": [],
        "index": idx,
    }
    report.update(index_checks(v, idx))
    report.update(folder_map_checks(v))
    dup_pages = {e.page.path for entries in idx.by_key().values() if len(entries) > 1 for e in entries}
    for page in idx.pages:
        if page.status == "ok":
            chars = len(read_text(v.root / page.path))
            if chars > limits["page_max_chars"]:
                report["over_size"].append((page.path, chars))
            # a page that shares a concept name stays unsealed until the duplicate is fixed
            if not seal_ok(v, verified, page) or page.path in dup_pages:
                report["unverified"].append(page)
    owners = idx.by_key()
    for name, rec in sorted(state["concepts"].items()):
        if norm_key(name) not in owners:
            report["orphans"].append(name)
        if int(rec.get("lapses") or 0) >= v.leech_at:
            report["leeches"].append((name, rec["lapses"]))
        gap = rec.get("gap")
        if gap and int(gap.get("fails") or 0) >= GAP_STUCK_FAILS:
            report["stuck_gaps"].append((name, gap["idea"], gap["fails"]))
    return report


def format_checks(v: Vault, report: dict, only=None) -> list:
    """The report as lines. With `only` (section keys), print just those sections
    and leave out the ones with nothing in them."""
    limits = v.limits

    def link_line(page):
        if page.status == "missing":
            return f"[[{page.link}]] (index line {page.line}): no such page"
        if page.status == "ambiguous":
            return f"[[{page.link}]] (index line {page.line}): ambiguous, use the path: {', '.join(page.candidates)}"
        return f"[[{page.link}]] (index line {page.line}): points to an excluded file ({page.path})"

    def misplaced_line(x):
        name, page, to, day = x
        return f"{name}: defined on {page}" + (f", belongs in {to}" if to else "") + f" (flagged {day})"

    sections = [
        ("over_size", f"Pages over {limits['page_max_chars']} characters", lambda x: f"{x[0]} ({x[1]} characters)"),
        ("duplicates", "Concept names used on more than one page", lambda x: f"{x[0]}: {', '.join(x[1])}"),
        ("long_descriptions", f"Descriptions over {limits['description_max_chars']} characters",
         lambda x: f"{x[0]} ({x[1]} characters)"),
        ("link_problems", "Index links that don't resolve", link_line),
        ("index_problems", "Index lines not understood", lambda x: f"line {x[0]}: {x[1]}"),
        ("orphans", "Concepts in review but not in the index (orphans)", lambda x: x),
        ("unindexed", "Pages not in the index (index them with /setup)", lambda x: x),
        ("unverified", "Indexed pages without a valid seal", lambda p: p.path),
        ("stale_seals", "Stale seals (the file is gone or is no longer a page; /maintenance removes them)", lambda x: x),
        ("thin", "Pages flagged as thin by /quiz", lambda x: f"{x[0]} (flagged {x[1]})"),
        ("misplaced", "Concepts flagged as misplaced by /quiz", misplaced_line),
        ("leeches", f"Leeches ({v.leech_at}+ lapses)", lambda x: f"{x[0]} ({x[1]} lapses)"),
        ("stuck_gaps", f"Gaps that won't close ({GAP_STUCK_FAILS}+ fails)", lambda x: f"{x[0]}: {x[1]} ({x[2]} fails)"),
        ("unmapped_folders", f"Folders with pages but no row in the folder map ({v.folder_map_rel})", lambda x: x),
        ("map_rows_without_folder", f"Folder map rows that name no top-level folder ({v.folder_map_rel})",
         lambda x: x),
    ]
    out = []
    for key, title, render in sections:
        if only is not None and key not in only:
            continue
        items = report[key]
        if only is not None and not items:
            continue
        out.append(f"{title}: {len(items) if items else 'none'}")
        for item in items:
            out.append(f"  - {render(item)}")
    return out


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------


def prune_empty_folders(root: Path, rel: str) -> list:
    """Remove the folders left empty by deleting `rel`, innermost first. Returns
    their vault-relative paths. Hidden folders and the vault itself stay."""
    removed = []
    parts = rel.split("/")[:-1]
    while parts and not any(part.startswith(".") for part in parts):
        try:
            (root.joinpath(*parts)).rmdir()
        except OSError:
            break
        removed.append("/".join(parts))
        parts.pop()
    return removed


def cmd_version(args):
    print(f"recall srs.py {VERSION}")


def cmd_init(args):
    root = (Path(args.vault) if args.vault else Path.cwd()).resolve()
    if (root / SRS_DIR / "config.json").exists():
        raise SrsError("Recall is already set up in this vault.", 2)
    if git_toplevel(root) is None:
        if not args.git_init:
            raise SrsError("This folder is not a git repository. Rerun with --git-init to create one.", 2)
        git(["init", "-q"], root)
        print(f"Created a git repository in {root}.")
    problem = git_identity_problem(root)
    if problem:
        raise SrsError(problem, 2)
    repo = Repo(root)
    if repo.status():
        repo.snapshot("vault: snapshot before Recall setup")
        print("Committed the vault's current content first (vault: snapshot before Recall setup).")

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if args.algorithm:
        config["scheduling"]["algorithm"] = args.algorithm
    if args.language:
        config["language"] = args.language
    if args.concept_language:
        config["concept_language"] = args.concept_language
    if args.inbox:
        config["paths"]["inbox"] = norm_rel(args.inbox)
    if args.index:
        config["paths"]["index"] = norm_rel(args.index)
    if args.new_pages:
        config["paths"]["new_pages"] = norm_rel(args.new_pages)
    if args.folder_map:
        config["paths"]["folder_map"] = norm_rel(args.folder_map)
    if args.new_page_placement:
        config["new_page_placement"] = args.new_page_placement
    if args.heading_level:
        if not 1 <= args.heading_level <= 6:
            raise SrsError("--heading-level must be between 1 and 6.")
        config["inbox_heading_level"] = args.heading_level
    config["exclude"] = [p for p in (args.exclude or []) if p.strip()]

    srs = root / SRS_DIR
    write_json(srs / "config.json", config)
    shutil.copyfile(str(TEMPLATES / "quiz-rules.md"), str(srs / "quiz-rules.md"))
    write_json(srs / "state.json", empty_state())
    write_json(srs / "verified.json", {})
    with (srs / "log.csv").open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerow(LOG_HEADER)
    paths = [f"{SRS_DIR}/config.json", f"{SRS_DIR}/quiz-rules.md", f"{SRS_DIR}/state.json",
             f"{SRS_DIR}/verified.json", f"{SRS_DIR}/log.csv"]
    for key, template in (("inbox", "inbox.md"), ("index", "index.md")):
        rel = config["paths"][key]
        target = root / rel
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(TEMPLATES / template), str(target))
            paths.append(rel)
            print(f"Created {rel}.")
    if ensure_gitignore(root):
        paths.append(".gitignore")
    sha = repo.commit(paths, "setup: initialize Recall")
    print(f"Scheduling algorithm: {config['scheduling']['algorithm']}.")
    print(f"New page placement: {config['new_page_placement']}.")
    print(f"Recall is set up. Committed {sha}: setup: initialize Recall.")


def cmd_begin(args):
    v = open_vault(args)
    repo = require_repo(v)
    session = load_session(v)
    if session is not None:
        raise SrsError(interrupted_report(repo, session), 3)
    snapped = repo.snapshot()
    session = {"command": args.command, "started": now_iso(), "touched": []}
    if args.command == "integrate":
        clean_review(v)  # leftovers of a session that never closed
        prune_supervision(v)
    suggestion_notes = hide_suggestions(v) if args.command == "quiz" else []
    save_session(v, session)
    note = f" Committed your manual edits first ({plural(snapped, 'file')}, vault: manual edits)." if snapped else ""
    print(f"Session open for /{args.command}.{note}")
    for line in suggestion_notes:
        print(line)


HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


def changed_index_lines(v: Vault, new_file: bool) -> set:
    """Line numbers of the index that the session added or altered since HEAD."""
    if new_file:
        return set(range(1, len(read_text(v.root / v.index_rel).splitlines()) + 1))
    out = git(["diff", "HEAD", "-U0", "--no-color", "--", v.index_rel], v.root).stdout
    lines = set()
    for m in HUNK_RE.finditer(out):
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) is not None else 1
        lines.update(range(start, start + count))
    return lines


def index_warnings(v: Vault, new_file: bool) -> list:
    """Index problems on the lines the session changed, checked against the whole index."""
    idx, _ = load_index(v)
    found = index_checks(v, idx, changed_index_lines(v, new_file))
    lines = format_checks(v, found, only=list(found))
    if lines:
        lines.insert(0, "Index warnings: fix these before asking the user to approve (the commit is still allowed).")
    return lines


def render_diff(v: Vault, repo: Repo, changed: dict, paths: list, warnings: bool = True) -> str:
    """The text `srs.py diff` prints for these changed paths; with warnings=False, without the index warnings."""
    content = [p for p in paths if not p.startswith(SRS_DIR + "/")]
    state_files = [p for p in paths if p.startswith(SRS_DIR + "/")]
    tracked = [p for p in content if changed[p] != "??"]
    untracked = [p for p in content if changed[p] == "??"]
    chunks = []
    if warnings and v.index_rel in content:
        found = index_warnings(v, changed[v.index_rel] == "??")
        if found:
            chunks += found + [""]
    if tracked:
        chunks.append(git(["diff", "HEAD", "--stat", "--"] + tracked, v.root).stdout.rstrip())
    for p in untracked:
        lines = len(read_text(v.root / p).splitlines())
        chunks.append(f" {p} (new file, {lines} lines)")
    chunks.append("")
    if tracked:
        chunks.append(git(["diff", "HEAD", "--"] + tracked, v.root).stdout.rstrip())
    for p in untracked:
        chunks.append(git(["diff", "--no-index", "--", os.devnull, p], v.root, check=False).stdout.rstrip())
    if state_files:
        chunks.append(f"\nState files also changed: {', '.join(state_files)}")
    return "\n".join(chunks).strip("\n")


def cmd_diff(args):
    v = open_vault(args)
    repo = require_repo(v)
    session = require_session(v)
    changed = repo.status()
    paths = [p for p in session.get("touched", []) if p in changed]
    warning = unrecorded_warning(repo, session)
    if warning:
        print("\n".join(warning + [""]))
    if not paths:
        print("No changes to review.")
        return
    print(render_diff(v, repo, changed, paths))


def cmd_commit(args):
    v = open_vault(args)
    repo = require_repo(v)
    session = require_session(v)
    paths = pending_paths(repo, session)
    message = args.message
    if args.review and paths:
        message += "\n\n" + review_commit_body(v, repo, session, args.review)
    session["touched"] = []
    if not paths:
        save_session(v, session)
        print("Nothing to commit.")
        return
    sha = repo.commit(paths, message)
    save_session(v, session)
    if args.review:
        shutil.rmtree(str(block_review_dir(v, args.review)), ignore_errors=True)
    print(f"Committed {sha}: {args.message} ({len(paths)} file{'s' if len(paths) != 1 else ''}).")


def discard_touched(v: Vault, repo: Repo, session: dict, include_state: bool):
    changed = repo.status()
    restored, removed, kept, folders = [], [], [], []
    for p in session.get("touched", []):
        if p.startswith(SRS_DIR + "/") and not include_state:
            kept.append(p)
            continue
        if p not in changed:
            continue
        if repo.in_head(p):
            git(["checkout", "HEAD", "--", p], v.root)
            restored.append(p)
        else:
            git(["rm", "--cached", "-q", "-f", "--ignore-unmatch", "--", p], v.root, check=False)
            target = v.root / p
            if target.exists():
                target.unlink()
            removed.append(p)
            folders += prune_empty_folders(v.root, p)
    session["touched"] = kept
    return restored, removed, folders


def cmd_discard(args):
    v = open_vault(args)
    repo = require_repo(v)
    session = require_session(v)
    restored, removed, folders = discard_touched(v, repo, session, include_state=False)
    save_session(v, session)
    if not restored and not removed:
        print("Nothing to discard.")
        return
    parts = []
    if restored:
        parts.append("restored " + ", ".join(restored))
    if removed:
        parts.append("removed new files " + ", ".join(removed))
    if folders:
        parts.append("removed empty folders " + ", ".join(folders))
    print("Discarded: " + "; ".join(parts) + ".")


def cmd_end(args):
    v = open_vault(args)
    session = load_session(v)
    if session is None:
        print("No session is open.")
        return
    repo = require_repo(v)
    pending = pending_paths(repo, session)
    if pending:
        raise SrsError("Uncommitted changes from this session: " + ", ".join(pending)
                       + ". Commit them (srs.py commit -m ...) or discard them (srs.py discard) first.", 2)
    session_path(v).unlink()
    clean_review(v)
    minutes = session_minutes(session) if session.get("command") == "quiz" else None
    duration = f" Duration: {minutes} min." if minutes is not None else ""
    print(f"Session for /{session.get('command')} closed.{duration}")


def cmd_abort(args):
    v = open_vault(args)
    session = load_session(v)
    if session is None:
        print("No session is open.")
        return
    repo = require_repo(v)
    restored, removed, folders = discard_touched(v, repo, session, include_state=True)
    session_path(v).unlink()
    clean_review(v)
    extra = f" and {len(folders)} empty folder(s)" if folders else ""
    print(f"Aborted /{session.get('command')}: restored {len(restored)} file(s), removed {len(removed)} new file(s){extra}.")


def cmd_status(args):
    v = open_vault(args)
    repo = require_repo(v)
    session = load_session(v)
    head = git(["log", "-1", "--format=%h %s"], v.root).stdout.strip()
    print(f"Vault: {v.root}\nStudy day: {v.today}\nLast commit: {head}")
    if session is None:
        print("Session: none")
    else:
        pending = pending_paths(repo, session)
        print(f"Session: /{session['command']} since {session['started']}; uncommitted: {', '.join(pending) or 'nothing'}")
    other = [p for p in repo.status() if session is None or p not in session.get("touched", [])]
    if other:
        print(f"Edits outside Recall (committed at the next command as vault: manual edits): {len(other)} file(s)")


def cmd_due(args):
    v = open_vault(args)
    state = load_state(v)
    idx, _ = load_index(v)
    for line in render_due(v, state, idx):
        print(line)


def apply_gap(updated: dict, before: dict, passed: bool, today: dt.date, idea) -> str:
    """Keep the concept's gap, the idea it failed, up to date after an answer. The schedule is
    untouched: the concept's own dates are the gap's dates, and while it is open its questions
    target it. Returns text for the answer's output.

    A question on new content (a focus) doesn't test the gap, so its pass leaves the gap open,
    and its fail replaces the gap when it names another idea."""
    gap = before.get("gap")
    tested = gap and not before.get("focus")
    if passed:
        last = parse_day(before.get("last"))
        if tested and last and (today - last).days >= GAP_CLOSE_DAYS:
            updated.pop("gap", None)
            return f" Gap closed: {gap['idea']}."
        return ""
    if tested:
        updated["gap"] = dict(gap, fails=int(gap.get("fails") or 1) + 1)
    elif idea:
        updated["gap"] = {"idea": short(idea, 200), "fails": 1}
    elif not gap:
        return ' No gap opened: run srs.py gap "<Concept>" "<the idea the answer missed>".'
    return ""


def cmd_record(args):
    v = open_vault(args)
    state = load_state(v)
    name = find_state_name(state, args.concept)
    if name is None:
        raise SrsError(f"'{args.concept}' is not in the review schedule.", 2)
    rec = state["concepts"][name]
    if rec.get("last") == v.today.isoformat():
        raise SrsError(f"{name} was already answered today; repeats in the same session don't count.", 2)
    neighbors = vault_neighbors(v)
    items, _ = daily_list(state, v.sched, v.today, neighbors)
    if name not in items:
        raise SrsError(f"{name} is not in today's list.", 2)
    before = rec["phase"]
    interval = int(rec.get("interval") or 0)
    # The snapshot lets `amend` undo this answer on the same study day; the next record replaces it.
    snapshot = copy.deepcopy({k: val for k, val in rec.items() if k != "undo"})
    updated = apply_answer(rec, args.result == "pass", v.today, v.sched, name)
    note = apply_gap(updated, snapshot, args.result == "pass", v.today, args.gap)
    if args.question:
        updated["questions"] = (list(rec.get("questions") or []) + [short(args.question, 200)])[-3:]
    updated["undo"] = {"day": v.today.isoformat(), "before": snapshot}
    state["concepts"][name] = updated
    save_state(v, state)
    append_log(v, [v.today.isoformat(), name, args.result, before, interval])
    print(describe_answer(name, before, updated, v.today, v.leech_at) + note)
    left, info = daily_list(state, v.sched, v.today, neighbors)
    total = info["done"] + len(left)
    if left:
        print(f"Next question: {info['done'] + 1}/{total}.")
    else:
        print(f"That was the last question ({total}/{total}).")


def rewrite_log_result(v: Vault, day: str, name: str, result: str):
    """Change the result of the last log line of `name` on `day`, leaving every other byte as it was.
    Returns the result that line had, or None when there is no such line."""
    path = v.srs / "log.csv"
    if not path.exists():
        return None
    with path.open(encoding="utf-8", newline="") as fh:
        lines = fh.read().split("\n")
    for i in range(len(lines) - 1, 0, -1):  # line 0 is the header
        body = lines[i].rstrip("\r")
        row = next(csv.reader([body]), [])
        if len(row) >= 3 and row[0] == day and row[1] == name:
            previous = row[2]
            row[2] = result
            out = io.StringIO()
            csv.writer(out, lineterminator="").writerow(row)
            lines[i] = out.getvalue() + lines[i][len(body):]
            with path.open("w", encoding="utf-8", newline="") as fh:
                fh.write("\n".join(lines))
            touch(v, f"{SRS_DIR}/log.csv")
            return previous
    return None


def cmd_amend(args):
    """Correct today's answer for a concept, e.g. after the user contests a verdict."""
    v = open_vault(args)
    state = load_state(v)
    name = find_state_name(state, args.concept)
    if name is None:
        raise SrsError(f"'{args.concept}' is not in the review schedule.", 2)
    rec = state["concepts"][name]
    today = v.today.isoformat()
    if rec.get("last") != today:
        raise SrsError(f"{name} was not answered today, so there is nothing to amend. "
                       "Use srs.py record if it is in today's list.", 2)
    undo = rec.get("undo")
    if not isinstance(undo, dict) or undo.get("day") != today or not isinstance(undo.get("before"), dict):
        raise SrsError(f"{name} has no snapshot from today to restore (it was answered before Recall 0.7.0). "
                       "Its answer stays as recorded.", 2)
    registered = None
    for row in reversed(read_log(v)):
        if row.get("day") == today and row.get("concept") == name:
            registered = row.get("result")
            break
    if registered is None:
        raise SrsError(f"{name} has no answer for today in .srs/log.csv, so there is nothing to amend.", 2)
    if registered == args.result:
        raise SrsError(f"{name} is already recorded as {args.result} today. "
                       "Amend it with the other result if the verdict was wrong.", 2)
    restored = copy.deepcopy(undo["before"])
    before = restored.get("phase")
    updated = apply_answer(restored, args.result == "pass", v.today, v.sched, name)
    note = apply_gap(updated, undo["before"], args.result == "pass", v.today, args.gap)
    updated["questions"] = list(rec.get("questions") or [])  # as they were after the record
    updated["undo"] = {"day": today, "before": undo["before"]}  # amending again starts from the same snapshot
    state["concepts"][name] = updated
    save_state(v, state)
    rewrite_log_result(v, today, name, args.result)
    print(describe_answer(name, before, updated, v.today, v.leech_at) + note)


def cmd_today(args):
    v = open_vault(args)
    t = v.today.isoformat()
    rows = [r for r in read_log(v) if r["day"] == t]
    passed = [r["concept"] for r in rows if r["result"] == "pass"]
    failed = [r["concept"] for r in rows if r["result"] == "fail"]
    state = load_state(v)
    print(f"Score: {len(passed)}/{len(rows)}")
    if failed:
        print("Failed: " + ", ".join(failed))
    leeches = [n for n in passed + failed
               if int(state["concepts"].get(n, {}).get("lapses") or 0) >= v.leech_at]
    if leeches:
        print("Leeches: " + ", ".join(sorted(set(leeches))))


def cmd_add(args):
    v = open_vault(args)
    idx, _ = load_index(v)
    owners = idx.by_key()
    state = load_state(v)
    missing = [n for n in args.concepts if norm_key(n) not in owners]
    if missing:
        raise SrsError("Not in the index (add them to the index first, or fix the names): " + ", ".join(missing), 2)
    added, skipped = [], []
    for raw in args.concepts:
        name = owners[norm_key(raw)][0].name
        if find_state_name(state, name) or name in added:
            skipped.append(name)
            continue
        state["concepts"][name] = new_record(args.source, v.today, state["next_order"])
        state["next_order"] += 1
        added.append(name)
    if added:
        save_state(v, state)
    print(f"Queued {len(added)}: {', '.join(added) or '-'}."
          + (f" Already scheduled: {', '.join(skipped)}." if skipped else ""))
    print(queue_summary(state, v))


def cmd_update(args):
    v = open_vault(args)
    state = load_state(v)
    name = find_state_name(state, args.concept)
    if name is None:
        raise SrsError(f"'{args.concept}' is not in the review schedule.", 2)
    state["concepts"][name] = apply_update(state["concepts"][name], v.today, short(args.focus, 300))
    save_state(v, state)
    print(f"{name}: updated; the next question targets the new content.")


def cmd_gap(args):
    """Open a gap by hand, or close one: its idea is wrong, or the page no longer supports it."""
    if bool(args.idea) == args.close:
        raise SrsError('Give the idea, or --close: srs.py gap "<Concept>" "<idea>" or srs.py gap "<Concept>" --close.')
    v = open_vault(args)
    state = load_state(v)
    name = find_state_name(state, args.concept)
    if name is None:
        raise SrsError(f"'{args.concept}' is not in the review schedule.", 2)
    rec = state["concepts"][name]
    if args.close:
        if not rec.get("gap"):
            print(f"{name} has no open gap.")
            return
        idea = rec.pop("gap")["idea"]
        save_state(v, state)
        print(f"{name}: gap closed: {idea}.")
        return
    rec["gap"] = {"idea": short(args.idea, 200), "fails": 1}
    save_state(v, state)
    print(f"{name}: gap open: {rec['gap']['idea']}.")


def cmd_rename(args):
    v = open_vault(args)
    state = load_state(v)
    old = find_state_name(state, args.old)
    if old is None:
        raise SrsError(f"'{args.old}' is not in the review schedule.", 2)
    idx, _ = load_index(v)
    owners = idx.by_key()
    if norm_key(args.new) not in owners:
        raise SrsError(f"Rename it in the index first: '{args.new}' is not in the index.", 2)
    new = owners[norm_key(args.new)][0].name
    if find_state_name(state, new) and norm_key(new) != norm_key(old):
        raise SrsError(f"'{new}' is already in the review schedule.", 2)
    state["concepts"][new] = state["concepts"].pop(old)
    save_state(v, state)
    print(f"Renamed {old} to {new}; its history is kept.")


def cmd_remove(args):
    v = open_vault(args)
    state = load_state(v)
    name = find_state_name(state, args.concept)
    if name is None:
        raise SrsError(f"'{args.concept}' is not in the review schedule.", 2)
    del state["concepts"][name]
    save_state(v, state)
    print(f"Removed {name} from the review schedule.")


def cmd_flag(args):
    v = open_vault(args)
    state = load_state(v)
    rel = norm_rel(args.page)
    if not (v.root / rel).exists():
        raise SrsError(f"No such page: {rel}", 2)
    if args.to and not args.misplaced:
        raise SrsError('--to goes with --misplaced: srs.py flag "<page>" --misplaced "<Concept>" --to "<where it belongs>".')
    if args.misplaced:
        idx, _ = load_index(v)
        owners = idx.by_key().get(norm_key(args.misplaced))
        if not owners:
            raise SrsError(f"'{args.misplaced}' is not in the index.", 2)
        name = owners[0].name
        state["flags"]["misplaced"][name] = {"page": rel, "to": short(args.to or "", 120), "day": v.today.isoformat()}
        save_state(v, state)
        print(f"Flagged {name} as misplaced on {rel}; /maintenance will report it.")
        return
    state["flags"]["thin"][rel] = v.today.isoformat()
    save_state(v, state)
    print(f"Flagged {rel} as thin; /maintenance will report it.")


def cmd_unflag(args):
    v = open_vault(args)
    state = load_state(v)
    if args.misplaced:
        key = norm_key(args.misplaced)
        found = [name for name in state["flags"]["misplaced"] if norm_key(name) == key]
        if not found:
            print(f"{args.misplaced} was not flagged as misplaced.")
            return
        del state["flags"]["misplaced"][found[0]]
        save_state(v, state)
        print(f"Removed the misplaced flag from {found[0]}.")
        return
    rel = norm_rel(args.page)
    if state["flags"]["thin"].pop(rel, None) is None:
        print(f"{rel} was not flagged.")
        return
    save_state(v, state)
    print(f"Removed the thin flag from {rel}.")


def retention_verdict(rate: float, target: list, knob: str) -> str:
    low, high = target
    if rate > 0.95:
        return f"above 95%: reviews come too early; consider raising {knob}"
    if rate < 0.80:
        return f"below 80%: reviews come too late; consider lowering {knob}"
    if low <= rate <= high:
        return "on target"
    return f"outside the {low:.0%}-{high:.0%} target, but close"


def pace_lines(v: Vault) -> list:
    """The daily pace and what it means for today's list."""
    sched = v.sched
    _, info = daily_list(load_state(v), sched, v.today, vault_neighbors(v))
    out = [
        f"New concepts per day: {sched['new_per_day']}. "
        f"Daily limit, reviews and new concepts together: {sched['daily_limit']}.",
        f"Today: {info['done']} done, {info['learning'] + info['reviews']} reviews and {info['new']} new to ask, "
        f"{info['carried']} carried over to tomorrow, {info['new_waiting']} new concepts waiting.",
    ]
    if info["carried"]:
        out.append("Due reviews exceed the daily limit, so no new concepts enter today: "
                   "raising new_per_day alone changes nothing until they clear.")
    if sched["new_per_day"] > sched["daily_limit"]:
        out.append("new_per_day is above the daily limit, so the daily limit caps new concepts.")
    return out


def review_mode_line(v: Vault) -> str:
    mode = v.integrate_review
    what = ("you approve each diff" if mode == "human"
            else "after the plan, agents review each block and you supervise only the blocks they send back")
    return f"/integrate review: {mode} ({what})."


def integrate_commits_since(v: Vault, sha) -> int:
    """Blocks integrated since a commit: `integrate:` commits, not the `integrate: queue` ones."""
    if not sha:
        return 0
    proc = git(["log", f"{sha}..HEAD", "--format=%s", "--", "."], v.root, check=False)
    if proc.returncode != 0:
        return 0  # the commit is gone (history rewritten): no reminder rather than a wrong one
    return sum(1 for line in proc.stdout.splitlines()
               if line.startswith("integrate:") and not line.startswith("integrate: queue"))


def review_prompt_status(v: Vault) -> str:
    """'first' until the user has answered, 'due' when a reminder is worth showing, else ''.
    Only the manual mode is asked about; nothing here is random, so it can be tested with --today."""
    if v.integrate_review != "human":
        return ""
    prompt = v.config["integrate_review_prompt"]
    if not prompt.get("asked"):
        return "first"
    try:
        waited = (v.today - parse_day(prompt["asked"])).days
    except ValueError:
        return ""
    if waited < int(prompt.get("wait_days") or prompt["first_days"]):
        return ""
    return "due" if integrate_commits_since(v, prompt.get("asked_head")) >= int(prompt["min_commits"]) else ""


def review_prompt_lines(v: Vault) -> list:
    status = review_prompt_status(v)
    prompt = v.config["integrate_review_prompt"]
    if status == "first":
        return ["", "## FIRST USE: the user hasn't chosen how /integrate is reviewed",
                "Before step 1, ask once, in the user's language and in two or three lines: keep approving every diff "
                "by hand (manual, the default), or approve only the plan and let judge agents check each block, with "
                "only the blocks they send back waiting for the user (automatic). Say it can be changed any time with "
                "/config. Record the answer with `srs.py config --integrate-review human` (manual) or "
                "`srs.py config --integrate-review agent` (automatic), and follow the mode the user chose, not the "
                "one shown above."]
    if status == "due":
        n = integrate_commits_since(v, prompt.get("asked_head"))
        return ["", "## Reminder due: the automatic review",
                f"The user has approved {plural(n, 'block')} by hand since {prompt['asked']}. After the closing line "
                "(step 6), add one line: automatic review exists (the user approves the plan, judge agents check the "
                "blocks), and ask whether to switch. Yes: `srs.py config --integrate-review agent`. No: "
                "`srs.py config --remind-later`, which postpones the next reminder. Don't mention it before then, "
                "and don't mention it again in this run."]
    return []


def cmd_config(args):
    v = open_vault(args)
    wanted = {}
    if args.new_per_day is not None:
        if args.new_per_day < 0:
            raise SrsError("--new-per-day must be 0 or more (0 pauses new concepts).")
        wanted["new_per_day"] = args.new_per_day
    if args.daily_limit is not None:
        if args.daily_limit < 1:
            raise SrsError("--daily-limit must be 1 or more.")
        wanted["daily_limit"] = args.daily_limit
    changes = {k: value for k, value in wanted.items() if v.sched[k] != value}
    mode, current = args.integrate_review, v.config.get("integrate_review")
    prompt = v.config["integrate_review_prompt"]
    summary = [f"{k} {v.sched[k]} -> {value}" for k, value in changes.items()]
    review_raw = {}  # what goes into the file for the integrate review settings
    if args.remind_later:
        if (mode or current) != "human":
            raise SrsError("--remind-later only applies while the /integrate review is manual (human).")
        wait = min(int(prompt.get("wait_days") or prompt["first_days"]) * 2, int(prompt["max_days"]))
        prompt_raw = {"asked": v.today.isoformat(), "asked_head": head_sha(v), "wait_days": wait}
        review_raw["prompt"] = prompt_raw
        summary.append(f"integrate_review reminder in {wait} days")
    if mode is not None and (mode != current or not prompt.get("asked")):
        # A first answer counts even when it keeps the manual mode: it ends the question.
        summary.append(f"integrate_review {current} -> {mode}" if mode != current else f"integrate_review {mode} kept")
        review_raw["mode"] = mode
        if not args.remind_later:
            review_raw["prompt"] = {"asked": v.today.isoformat(), "asked_head": head_sha(v),
                                    "wait_days": int(prompt["first_days"])}
    if (wanted or mode is not None or args.remind_later) and not summary:
        print("Nothing changed.")
    if summary:
        repo = require_repo(v)
        path = v.srs / "config.json"
        raw = read_json(path, {})  # the file as written: load_config's defaults stay out of it
        for key, value in changes.items():
            raw.setdefault("scheduling", {})[key] = value
        if "mode" in review_raw:
            raw["integrate_review"] = review_raw["mode"]
            v.config["integrate_review"] = review_raw["mode"]
        if "prompt" in review_raw:
            raw.setdefault("integrate_review_prompt", {}).update(review_raw["prompt"])
            prompt.update(review_raw["prompt"])
        write_json(path, raw)
        text = ", ".join(summary)
        # Path-limited, so an open session's edits stay out and its discard can't revert this.
        sha = repo.commit([f"{SRS_DIR}/config.json"], f"config: {text}")
        v.sched.update(changes)
        print(f"Committed {sha}: config: {text}.")
    print("\n".join(pace_lines(v) + [review_mode_line(v)]))


def pass_rate(rows: list) -> float:
    return sum(1 for r in rows if r["result"] == "pass") / len(rows)


def cmd_stats(args):
    v = open_vault(args)
    state = load_state(v)
    rows = read_log(v)
    start = v.today - days(29)
    recent = [r for r in rows if parse_day(r["day"]) >= start]
    # Only answers given in the review phase: the log stores the phase before the answer.
    reviews = [r for r in recent if r["phase"] == "review"]
    name, _, entry = algorithm(v.sched)
    knob = f"scheduling.{name}.{entry.growth_key}"
    mature_at = int(v.sched["mature_interval"])
    print(f"Study day: {v.today}")
    print(f"Scheduling algorithm: {name}")
    if not reviews:
        print("Retention on reviews, last 30 days: no reviews yet")
    else:
        known = [r for r in reviews if (r.get("interval") or "").strip()]
        young = [r for r in known if int(r["interval"]) < mature_at]
        mature = [r for r in known if int(r["interval"]) >= mature_at]
        print("Retention on reviews, last 30 days:")
        for label, group in ((f"young (interval under {mature_at} days)", young),
                             (f"mature (interval of {mature_at} days or more)", mature)):
            if len(group) < 30:
                print(f"  {label}: not enough data ({len(group)} of the 30 needed)")
            else:
                rate = pass_rate(group)
                print(f"  {label}: {rate:.0%} of {len(group)} "
                      f"({retention_verdict(rate, v.config['target_retention'], knob)})")
        print(f"  combined: {pass_rate(reviews):.0%} of {len(reviews)}")
        if len(known) < len(reviews):
            print(f"  ({len(reviews) - len(known)} older review(s) logged without an interval count only in combined)")
        if len(mature) >= 30:
            print("Calibrate on mature retention.")
        elif len(young) >= 30:
            print("Calibrate on young retention until mature has 30 reviews.")
        else:
            print("Too few reviews to calibrate yet.")
    counts = defaultdict(int)
    for rec in state["concepts"].values():
        counts[rec["phase"]] += 1
    print("Concepts: " + ", ".join(f"{p} {counts[p]}" for p in ("new", "learning", "review", "relearning")))
    print(queue_summary(state, v))
    print(f"Open gaps: {sum(1 for r in state['concepts'].values() if r.get('gap'))}")
    leeches = [(n, r["lapses"]) for n, r in state["concepts"].items()
               if int(r.get("lapses") or 0) >= v.leech_at]
    if leeches:
        print("Leeches: " + ", ".join(f"{n} ({k})" for n, k in sorted(leeches)))


def cmd_check(args):
    v = open_vault(args)
    for line in format_checks(v, run_checks(v)):
        print(line)


def find_index_page(idx: Index, target: str):
    rel = norm_rel(target)
    for page in idx.pages:
        if page.path and page.path.casefold() == rel.casefold():
            return page
    link = rel[:-3] if rel.lower().endswith(".md") else rel
    for page in idx.pages:
        if page.link.casefold() == link.casefold():
            return page
    return None


def cmd_verify(args):
    v = open_vault(args)
    idx, _ = load_index(v)
    owners = idx.by_key()
    verified = load_verified(v)
    sealed, refused = [], []
    for target in args.pages:
        page = find_index_page(idx, target)
        if page is None:
            refused.append(f"{target}: not in the index")
            continue
        if page.status != "ok":
            refused.append(f"{target}: its index link is {page.status}")
            continue
        data = (v.root / page.path).read_bytes()
        chars = len(data.decode("utf-8", errors="replace"))
        if chars > v.limits["page_max_chars"]:
            refused.append(f"{page.path}: {chars} characters, over the limit")
            continue
        shared = [c.name for c in page.concepts if len(owners[norm_key(c.name)]) > 1]
        if shared:
            refused.append(f"{page.path}: concept names also used elsewhere: {', '.join(shared)}")
            continue
        verified[page.path] = {"blob": blob_hash(data), "entry": page.entry_hash(), "day": v.today.isoformat()}
        sealed.append(page.path)
    if sealed:
        write_json(v.srs / "verified.json", verified)
        touch(v, f"{SRS_DIR}/verified.json")
    print(f"Sealed {len(sealed)}: {', '.join(sealed) or '-'}")
    if refused:
        print("Refused:\n" + "\n".join(f"  - {r}" for r in refused))
        raise SystemExit(2)


def cmd_unseal(args):
    v = open_vault(args)
    idx, _ = load_index(v)
    verified = load_verified(v)
    removed = []
    for target in args.pages:
        page = find_index_page(idx, target)
        rel = page.path if page and page.path else norm_rel(target)
        if verified.pop(rel, None) is not None:
            removed.append(rel)
    if removed:
        write_json(v.srs / "verified.json", verified)
        touch(v, f"{SRS_DIR}/verified.json")
    print(f"Unsealed {len(removed)}: {', '.join(removed) or '-'}")


def cmd_prune_seals(args):
    v = open_vault(args)
    verified = load_verified(v)
    stale = stale_seals(v, verified)
    for rel in stale:
        del verified[rel]
    if stale:
        write_json(v.srs / "verified.json", verified)
        touch(v, f"{SRS_DIR}/verified.json")
    print(f"Removed {plural(len(stale), 'stale seal')}: {', '.join(stale) or '-'}")


def cmd_inbox(args):
    v = open_vault(args)
    rel, path = block_source(v, args.file)
    if not path.exists():
        raise SrsError(f"{rel} does not exist.")
    level = v.config["inbox_heading_level"]
    text = read_text(path)
    blocks, lines = parse_blocks(text, level)
    if args.action == "list":
        print(f"{rel}: {len(blocks)} block(s), newest first")
        for b in reversed(blocks):
            print(f"  [{b.id}] {b.title} ({len(b.text)} characters)")
        return
    if not args.ids:
        raise SrsError("Give one or more block ids.")
    by_id = {b.id: b for b in blocks}
    missing = [i for i in args.ids if i not in by_id]
    if missing:
        raise SrsError(f"No block with id {', '.join(missing)} in {rel} (it changed or was already removed).", 2)
    if args.action == "show":
        if len(args.ids) == 1:
            print(by_id[args.ids[0]].text.rstrip("\n"))
            return
        for n, i in enumerate(args.ids):  # several blocks: label each with its id, since titles can repeat
            print(f"{'' if n == 0 else chr(10)}[{i}]")
            print(by_id[i].text.rstrip("\n"))
        return
    drop = set()
    for i in args.ids:
        drop.update(range(by_id[i].start, by_id[i].end))
    new_text = "".join(line for n, line in enumerate(lines) if n not in drop)
    if max(drop) == len(lines) - 1:  # the end of the file went: drop the blank lines left behind
        stripped = new_text.rstrip()
        new_text = stripped + ("\r\n" if "\r\n" in text else "\n") if stripped else ""
    write_text(path, new_text)
    touch(v, rel)
    forget_supervision(v, args.ids)
    left = len(blocks) - len(args.ids)
    print(f"Removed {len(args.ids)} block(s) from {rel}; {left} left.")


def cmd_setup_batch(args):
    v = open_vault(args)
    idx, _ = load_index(v)
    indexed = {p.path for p in idx.pages if p.path}
    left = [p for p in content_pages(v) if p not in indexed]
    batch = left[: v.limits["setup_batch"]]
    print(f"Pages not in the index: {len(left)}. Next batch ({len(batch)}):")
    for rel in batch:
        print(f"  {rel} ({len(read_text(v.root / rel))} characters)")


# Strings of the welcome message. Pairs are (singular, plural).
WELCOME_STRINGS = {
    "pt": {
        "interrupted": "Recall: sessão interrompida! Rode um comando do Recall para recuperá-la.",
        "today": "Recall: hoje {total}: {parts}",
        "left": ("Recall: hoje falta {total}: {parts}", "Recall: hoje faltam {total}: {parts}"),
        "done": "Recall: quiz de hoje feito: {passed}/{answered}",
        "next": "Recall: nada para hoje. Próximo quiz {when}",
        "tomorrow": "amanhã",
        "in_days": "em {n} dias",
        "nothing": "Recall: nenhuma revisão agendada",
        "paused": "Recall: nada para hoje. {waiting}, mas o ritmo diário está em 0 (mude com /config)",
        "waiting": ("concept aguardando", "concepts aguardando"),
        "quiz": "fazer o quiz de hoje",
        "quiz_left": "continuar o quiz de hoje",
        "inbox": "{blocks} no inbox: {titles}",
        "question": ("pergunta", "perguntas"),
        "review": ("revisão", "revisões"),
        "new": ("concept novo", "concepts novos"),
        "block": ("bloco", "blocos"),
        "changed": ("{pages} mudou desde a última verificação ({ago})", "{pages} mudaram desde a última verificação ({ago})"),
        "unchecked": "{pages} sem verificação",
        "ago_today": "hoje",
        "ago": ("há {n} dia", "há {n} dias"),
        "page": ("página", "páginas"),
        "stuck": ("lacuna que não fecha", "lacunas que não fecham"),
        "leech": ("leech", "leeches"),
        "supervision": "{n} aguardando supervisão",
    },
    "en": {
        "interrupted": "Recall: interrupted session! Run a Recall command to recover it.",
        "today": "Recall: today {total}: {parts}",
        "left": ("Recall: {total} left today: {parts}", "Recall: {total} left today: {parts}"),
        "done": "Recall: today's quiz done: {passed}/{answered}",
        "next": "Recall: nothing to ask today. Next quiz {when}",
        "tomorrow": "tomorrow",
        "in_days": "in {n} days",
        "nothing": "Recall: no reviews scheduled",
        "paused": "Recall: nothing to ask today. {waiting}, but the daily pace is 0 (change it with /config)",
        "waiting": ("concept waiting", "concepts waiting"),
        "quiz": "take today's quiz",
        "quiz_left": "continue today's quiz",
        "inbox": "{blocks} in the inbox: {titles}",
        "question": ("question", "questions"),
        "review": ("review", "reviews"),
        "new": ("new concept", "new concepts"),
        "block": ("block", "blocks"),
        "changed": ("{pages} changed since the last check ({ago})", "{pages} changed since the last check ({ago})"),
        "unchecked": "{pages} not checked yet",
        "ago_today": "today",
        "ago": ("{n} day ago", "{n} days ago"),
        "page": ("page", "pages"),
        "stuck": ("gap that won't close", "gaps that won't close"),
        "leech": ("leech", "leeches"),
        "supervision": "{n} waiting for supervision",
    },
}
WELCOME_MAX_TITLES = 3
MAINTENANCE_PAGES = 5  # the welcome suggests /maintenance with this many pages without a valid seal
MAINTENANCE_DAYS = 14  # or with any such page, this long after the last seal


def welcome_strings(language) -> dict:
    """The language setting is free text: Portuguese when it names it ("Brazilian Portuguese", "pt-BR"), else English."""
    value = str(language or "").strip().casefold()
    return WELCOME_STRINGS["pt" if "portugu" in value or value.startswith("pt") else "en"]


def welcome_count(words: dict, n: int, word: str) -> str:
    return f"{n} {words[word][0 if n == 1 else 1]}"


def welcome_inbox(v: Vault, words: dict):
    """The /integrate line's text, newest block first like `inbox list`; None when the inbox is empty."""
    _, path = block_source(v, None)
    if not path.is_file():
        return None
    blocks, _ = parse_blocks(read_text(path), v.config["inbox_heading_level"])
    if not blocks:
        return None

    titles = [short(b.title, 40) for b in reversed(blocks) if b.title][:WELCOME_MAX_TITLES]
    rest = len(blocks) - len(titles)
    if rest:
        titles.append(f"+{rest}")
    text = words["inbox"].format(blocks=welcome_count(words, len(blocks), "block"), titles=", ".join(titles))
    try:
        ids = {b.id for b in blocks}
        waiting = sum(1 for block_id, e in load_supervision(v).items()
                      if block_id in ids and (e.get("source") or v.inbox_rel) == v.inbox_rel)
    except Exception:  # the parked blocks are extra: never lose the inbox line over them
        waiting = 0
    return f"{text} ({words['supervision'].format(n=waiting)})" if waiting else text


def welcome_maintenance(v: Vault, state: dict, words: dict):
    """The /maintenance line's text, only when the command is worth running; None otherwise.
    Any edit breaks a page's seal, so a line for every unsealed page would soon go unread.
    The last check is the newest seal: the git log can't tell, since other commits use the prefix."""
    idx, _ = load_index(v)
    verified = load_verified(v)
    pending = sum(1 for page in idx.pages if page.status == "ok" and not seal_ok(v, verified, page))
    sealed = [day for day in (parse_day(r.get("day")) for r in verified.values()) if day]
    age = max(0, (v.today - max(sealed)).days) if sealed else None
    stuck = sum(1 for r in state["concepts"].values()
                if r.get("gap") and int(r["gap"].get("fails") or 0) >= GAP_STUCK_FAILS)
    leeches = sum(1 for r in state["concepts"].values() if int(r.get("lapses") or 0) >= v.leech_at)
    worth_it = pending >= MAINTENANCE_PAGES or (pending and age is not None and age >= MAINTENANCE_DAYS)
    if not (worth_it or stuck or leeches):
        return None
    parts = []
    if pending:
        pages = welcome_count(words, pending, "page")
        if age is None:
            parts.append(words["unchecked"].format(pages=pages))
        else:
            ago = words["ago_today"] if age == 0 else words["ago"][0 if age == 1 else 1].format(n=age)
            parts.append(words["changed"][0 if pending == 1 else 1].format(pages=pages, ago=ago))
    if stuck:
        parts.append(welcome_count(words, stuck, "stuck"))
    if leeches:
        parts.append(welcome_count(words, leeches, "leech"))
    return "; ".join(parts)


def welcome_message(v: Vault) -> str:
    """The status shown when a session starts. Reads the vault's files only: no git, no writes."""
    words = welcome_strings(v.config.get("language"))

    state = load_state(v)
    neighbors = vault_neighbors(v)
    items, info = daily_list(state, v.sched, v.today, neighbors)
    t = v.today.isoformat()
    rows = [r for r in read_log(v) if r["day"] == t]

    lines = []
    if session_path(v).exists():
        lines.append(words["interrupted"])
    if items:
        reviews = info["learning"] + info["reviews"]
        parts = ", ".join(welcome_count(words, n, w) for n, w in ((reviews, "review"), (info["new"], "new")) if n)
        total = welcome_count(words, len(items), "question")
        template = words["left"][0 if len(items) == 1 else 1] if rows else words["today"]
        lines.append(template.format(total=total, parts=parts))
        lines.append(f"  {'/quiz':<13}{words['quiz_left' if rows else 'quiz']}")
    elif rows:
        passed = sum(1 for r in rows if r["result"] == "pass")
        lines.append(words["done"].format(passed=passed, answered=len(rows)))
    else:
        nxt, _ = next_due(state, v.sched, v.today, neighbors)
        if nxt is None and state["concepts"]:
            lines.append(words["paused"].format(waiting=welcome_count(words, len(state["concepts"]), "waiting")))
        elif nxt is None:
            lines.append(words["nothing"])
        else:
            delta = (nxt - v.today).days
            when = words["tomorrow"] if delta == 1 else words["in_days"].format(n=delta)
            lines.append(words["next"].format(when=when))
    inbox = welcome_inbox(v, words)
    if inbox:
        lines.append(f"  {'/integrate':<13}{inbox}")
    try:
        maintenance = welcome_maintenance(v, state, words)
    except Exception:  # a broken index or an unreadable page is /maintenance's to report, not a reason to lose the rest
        maintenance = None
    if maintenance:
        lines.append(f"  {'/maintenance':<13}{maintenance}")
    return "\n".join(lines)


def read_hook_json() -> dict:
    """The JSON a hook receives on stdin; empty when there is none or it is not an object."""
    try:
        if not sys.stdin.isatty():
            data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
            return data if isinstance(data, dict) else {}
    except (ValueError, AttributeError, OSError):
        pass
    return {}


def hook_folder(data=None) -> str:
    """The folder a hook runs for: cwd from its stdin JSON, then the project dir, then here."""
    if data is None:
        data = read_hook_json()
    return data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def cmd_welcome(args):
    """SessionStart hook. Whatever goes wrong must not reach the session: no output, exit 0."""
    try:
        args.vault = args.vault or hook_folder()
        # The leading newline keeps the first line apart from the label the host prints before it.
        print(json.dumps({"systemMessage": "\n" + welcome_message(open_vault(args))}))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Agent review (integrate_review: agent)
# ---------------------------------------------------------------------------
# The user approves the plan; after that each block is written, checked here, judged
# by read-only agents and committed without a diff to approve. A block that doesn't
# pass is parked for the user. State lives in .srs/review/ and is never committed.


class VerdictError(ValueError):
    """A judge's verdict that doesn't follow the format; the message tells the judge what to fix."""


def review_dir(v: Vault) -> Path:
    return v.srs / REVIEW_DIR


def ensure_review_dir(v: Vault) -> Path:
    path = review_dir(v)
    path.mkdir(parents=True, exist_ok=True)
    ignore = path / ".gitignore"  # keeps the folder out of git in vaults whose .gitignore predates it
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    return path


def block_review_dir(v: Vault, block_id: str) -> Path:
    if not BLOCK_ID_RE.fullmatch(block_id):
        raise SrsError(f"'{block_id}' is not a block id.")
    return review_dir(v) / block_id


def clean_review(v: Vault) -> None:
    """Forget the plan and the per-block files; what waits for the user's supervision stays."""
    path = review_dir(v)
    if not path.is_dir():
        return
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(str(child), ignore_errors=True)
        elif child.name == "plan.md":
            child.unlink()


def source_blocks(v: Vault, file_arg):
    rel, path = block_source(v, file_arg)
    if not path.exists():
        raise SrsError(f"{rel} does not exist.")
    blocks, _ = parse_blocks(read_text(path), v.config["inbox_heading_level"])
    return rel, blocks


def find_block(v: Vault, block_id: str, file_arg):
    rel, blocks = source_blocks(v, file_arg)
    for b in blocks:
        if b.id == block_id:
            return rel, b
    raise SrsError(f"No block with id {block_id} in {rel} (it changed or was already removed).", 2)


# -- Blocks waiting for the user ---------------------------------------------


def supervision_path(v: Vault) -> Path:
    return review_dir(v) / "supervision.json"


def load_supervision(v: Vault) -> dict:
    return read_json(supervision_path(v), {})


def save_supervision(v: Vault, data: dict) -> None:
    if data:
        ensure_review_dir(v)
        write_json(supervision_path(v), data)
    elif supervision_path(v).exists():
        supervision_path(v).unlink()


def live_supervision(v: Vault) -> dict:
    """The parked blocks that are still in their source file (the others were integrated or edited)."""
    found, ids = {}, {}
    for block_id, entry in load_supervision(v).items():
        source = entry.get("source") or v.inbox_rel
        if source not in ids:
            try:
                ids[source] = {b.id for b in source_blocks(v, source)[1]}
            except SrsError:
                ids[source] = set()
        if block_id in ids[source]:
            found[block_id] = entry
    return found


def prune_supervision(v: Vault) -> None:
    current = load_supervision(v)
    live = live_supervision(v)
    if live != current:
        save_supervision(v, live)


def forget_supervision(v: Vault, ids: list) -> None:
    current = load_supervision(v)
    if any(i in current for i in ids):
        save_supervision(v, {k: e for k, e in current.items() if k not in ids})


# -- What the diff did -------------------------------------------------------


def split_lines(text: str) -> list:
    lines = text.replace("\r\n", "\n").split("\n")
    return lines[:-1] if lines and lines[-1] == "" else lines


def file_changes(v: Vault, repo: Repo, rel: str, status: str) -> tuple:
    """(added lines, removed lines) of one changed file against HEAD."""
    path = v.root / rel
    if status == "??" or not repo.in_head(rel):
        return (split_lines(read_text(path)) if path.exists() else []), []
    out = git(["diff", "HEAD", "-U0", "--no-color", "--", rel], v.root).stdout
    added, removed, in_hunk = [], [], False
    for raw in out.split("\n"):
        raw = raw.rstrip("\r")
        if raw.startswith("@@"):
            in_hunk = True
        elif in_hunk and raw.startswith("+"):
            added.append(raw[1:])
        elif in_hunk and raw.startswith("-"):
            removed.append(raw[1:])
    return added, removed


def new_index_concepts(v: Vault) -> list:
    """Concepts of the index that HEAD's index doesn't have."""
    idx, _ = load_index(v)
    head = git(["show", f"HEAD:./{v.index_rel}"], v.root, check=False)
    old = {norm_key(c.name) for c in parse_index(head.stdout).concepts()} if head.returncode == 0 else set()
    return [c.name for c in idx.concepts() if norm_key(c.name) not in old]


def review_facts(v: Vault, repo: Repo, session: dict, source_rel: str) -> dict:
    """What the session changed, ready for the judges. The source file (the inbox) is left out."""
    changed = repo.status()
    paths = [p for p in session.get("touched", [])
             if p in changed and not p.startswith(SRS_DIR + "/") and p != source_rel]
    files = {p: file_changes(v, repo, p, changed[p]) for p in paths}
    added_all = {line.strip() for added, _ in files.values() for line in added if line.strip()}
    moved, rewritten, index_removed = [], [], []
    for p, (_, removed) in files.items():
        for line in removed:
            if not line.strip():
                continue
            if line.strip() in added_all:
                moved.append((p, line))
            elif p == v.index_rel:
                index_removed.append(line)
            else:
                rewritten.append((p, line))
    moved_text = {line.strip() for _, line in moved}
    headings = [(p, line.strip()) for p, (added, _) in files.items() if p != v.index_rel
                for line in added if HEADING_RE.match(line) and line.strip() not in moved_text]
    concepts = new_index_concepts(v) if v.index_rel in files else []
    digest = hashlib.sha1(json.dumps(sorted(files.items()), ensure_ascii=False).encode("utf-8")).hexdigest()
    return {"status": changed, "files": files, "moved": moved, "rewritten": rewritten,
            "index_removed": index_removed, "headings": headings, "concepts": concepts, "digest": digest}


def page_headings(path: Path) -> set:
    found, fence = set(), FenceTracker()
    for raw in split_lines(read_text(path)):
        if fence.step(raw):
            continue
        m = re.match(r"^#{1,6}[ \t]+(.*?)[ \t#]*$", raw)
        if m:
            found.add(norm_key(m.group(1)))
    return found


def review_problems(v: Vault, facts: dict, plan: str) -> list:
    """The checks the script runs itself; any problem sends the block back to the writer."""
    problems = []
    files, changed = facts["files"], facts["status"]
    if v.index_rel in files:
        problems += [line.strip() for line in index_warnings(v, changed[v.index_rel] == "??")[1:]]
    moved_text = {line.strip() for _, line in facts["moved"]}
    tables, headings = link_tables(v), {}
    for p, (added, _) in files.items():
        if p == v.index_rel:
            continue
        for line in added:
            if line.strip() in moved_text:
                continue
            for m in WIKILINK_RE.finditer(line):
                target, _, heading = m.group(1).split("|", 1)[0].partition("#")
                target, heading = target.strip(), heading.strip()
                if target.rsplit(".", 1)[-1].lower() in ATTACHMENT_EXTS and "." in target:
                    continue
                cands = [p] if not target else link_candidates(tables, target)
                if len(cands) != 1:
                    problems.append(f"{p}: [[{m.group(1)}]] {'is ambiguous' if cands else 'does not resolve'}")
                    continue
                last = heading.split("#")[-1].strip()
                if last and not last.startswith("^"):
                    if cands[0] not in headings:
                        headings[cands[0]] = page_headings(v.root / cands[0])
                    if norm_key(last) not in headings[cands[0]]:
                        problems.append(f"{p}: [[{m.group(1)}]] points to a heading that {cands[0]} doesn't have")
    plan_text = plan.casefold()
    rewritten = defaultdict(int)
    for p, _ in facts["rewritten"]:
        rewritten[p] += 1
    for p, n in sorted(rewritten.items()):
        if Path(p).stem.casefold() not in plan_text:
            problems.append(f"{p}: {plural(n, 'existing line')} rewritten or removed, but the approved plan "
                            "doesn't name that page (the plan must say 'move', 'rewrite' or 'link')")
    return problems


def judges_for(facts: dict) -> list:
    """The concept judge is needed only when the diff gives a page a new heading or the index a new concept."""
    return ["diff-judge"] + (["concept-judge"] if facts["headings"] or facts["concepts"] else [])


def listing(items: list, fmt) -> str:
    return "\n".join(f"- {fmt(item)}" for item in items) if items else "(none)"


def review_input_text(v: Vault, repo: Repo, block: Block, plan: str, facts: dict, judge: str) -> str:
    """The file one judge reads. The diff judge gets the pages only: the index is the concept judge's."""
    with_index = judge != "diff-judge"
    paths = [p for p in facts["files"] if with_index or p != v.index_rel]
    diff = render_diff(v, repo, facts["status"], paths, warnings=False) if paths else "(no changes)"
    map_path = v.root / v.folder_map_rel
    head = [
        f"# Review input: block {block.id} ({block.title})",
        f"Block id: {block.id}",
        f"Vault: {v.root.as_posix()}",
    ]
    if with_index:
        head.append(f"Index: {(v.root / v.index_rel).as_posix()}")
    head += [
        f"Folder map: {map_path.as_posix() if map_path.is_file() else 'none'}",
        "Pages are vault-relative below; open one by joining it to the vault path above.",
    ]
    if not with_index:
        head.append("The index is judged by another agent: its changes are left out of this file.")
    derived = [
        "## Derived from the diff",
        "Moved lines (removed from one place and added unchanged elsewhere):",
        listing(facts["moved"], lambda x: f"{x[0]}: {x[1].strip()}"),
        "",
        "Existing lines rewritten or removed:",
        listing(facts["rewritten"], lambda x: f"{x[0]}: {x[1].strip()}"),
        "",
    ]
    if with_index:
        derived += ["Index lines changed or removed:", listing(facts["index_removed"], lambda x: x.strip()), ""]
    derived += ["New headings:", listing(facts["headings"], lambda x: f"{x[0]}: {x[1]}"), ""]
    if with_index:
        derived += ["New concepts in the index:", listing(facts["concepts"], lambda x: x), ""]
    return "\n".join(head + [
        "",
        "## Block text (from the inbox)",
        "----- begin block text -----",
        block.text.rstrip("\n"),
        "----- end block text -----",
        "",
        "## Approved plan",
        "----- begin plan -----",
        plan.rstrip("\n"),
        "----- end plan -----",
        "",
    ] + derived + [
        "## Diff",
        "----- begin diff -----",
        diff,
        "----- end diff -----",
        "",
    ])


def write_review_inputs(v: Vault, repo: Repo, block: Block, plan: str, facts: dict, judges: list) -> dict:
    """{judge: path} of the input file each judge reads."""
    paths = {}
    for judge in judges:
        path = block_review_dir(v, block.id) / f"input-{judge}.md"
        write_text(path, review_input_text(v, repo, block, plan, facts, judge))
        paths[judge] = path
    return paths


def load_meta(v: Vault, block_id: str) -> dict:
    meta = read_json(block_review_dir(v, block_id) / "meta.json", None)
    if not meta:
        raise SrsError(f"Block {block_id} has no review in progress: run srs.py review prepare {block_id}.", 2)
    return meta


def report_checks(problems: list) -> list:
    if not problems:
        return ["Checks: ok"]
    return ["Checks: FAIL"] + [f"  - {p}" for p in problems]


# -- The judges' verdict -----------------------------------------------------


def parse_verdict(text: str) -> dict:
    """Check a judge's answer against the format; returns block, judge, verdict and the result of each letter."""
    lines = [line.strip() for line in text.strip().splitlines()]
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    lines = [line for line in lines if line]
    if len(lines) < 4:
        raise VerdictError("it needs the lines BLOCK, JUDGE, VERDICT and one line per criterion")
    m = re.fullmatch(r"BLOCK (%s)" % BLOCK_ID_RE.pattern, lines[0])
    if not m:
        raise VerdictError("the first line must be 'BLOCK <id>'")
    block_id = m.group(1)
    m = re.fullmatch(r"JUDGE (%s)" % "|".join(JUDGES), lines[1])
    if not m:
        raise VerdictError("the second line must be 'JUDGE diff-judge' or 'JUDGE concept-judge'")
    judge = m.group(1)
    m = re.fullmatch(r"VERDICT (PASS|REVISE)", lines[2])
    if not m:
        raise VerdictError("the third line must be 'VERDICT PASS' or 'VERDICT REVISE'")
    verdict = m.group(1)
    letters = JUDGES[judge]
    results = {}
    for line in lines[3:]:
        m = re.fullmatch(r"([A-Z]) PASS", line)
        if m:
            letter, status, detail = m.group(1), "PASS", None
        else:
            m = re.fullmatch(r"([A-Z]) FAIL\s*[—–-]+\s*(\S.*)", line)
            if not m:
                raise VerdictError(f"'{line}' is not '<letter> PASS' or '<letter> FAIL — <path>: \"<line>\" — <problem>'")
            letter, status, detail = m.group(1), "FAIL", m.group(2)
        if letter not in letters:
            raise VerdictError(f"{judge} answers the criteria {', '.join(letters)}, not {letter}")
        if letter in results and (status == "PASS" or results[letter][0] == "PASS"):
            raise VerdictError(f"criterion {letter} appears twice, as PASS and as another line")
        results.setdefault(letter, (status, []))[1].append(detail)
    missing = [c for c in letters if c not in results]
    if missing:
        raise VerdictError(f"criterion {', '.join(missing)} is missing")
    failed = [c for c, (status, _) in results.items() if status == "FAIL"]
    if bool(failed) != (verdict == "REVISE"):
        raise VerdictError("VERDICT must be REVISE exactly when some criterion has a FAIL line")
    return {"block": block_id, "judge": judge, "verdict": verdict, "failed": sorted(failed), "results": results}


def stored_verdict(v: Vault, block_id: str, judge: str):
    """The parsed verdict captured for a judge, or None when there is none (or it no longer parses)."""
    data = read_json(block_review_dir(v, block_id) / f"{judge}.json", None)
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        return None
    try:
        parsed = parse_verdict(data["text"])
    except VerdictError:
        return None
    return parsed if parsed["block"] == block_id and parsed["judge"] == judge else None


def capture_verdict(args) -> None:
    data = read_hook_json()
    args.vault = args.vault or hook_folder(data)
    v = open_vault(args)
    session = load_session(v)
    if not session or session.get("command") != "integrate":
        return
    event = data.get("hook_event_name")
    if os.environ.get("RECALL_HOOK_LOG") == "1":
        ensure_review_dir(v)
        with (review_dir(v) / "hook-log.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False) + "\n")
    agent_type = data.get("agent_type") or ""
    judge = agent_type[len(JUDGE_PLUGIN) + 1:] if agent_type.startswith(JUDGE_PLUGIN + ":") else None
    if judge not in JUDGES:
        return
    agent_id = data.get("agent_id")
    if event == "SubagentStop":
        if agent_id:  # the report already came through the handback tool
            for path in review_dir(v).glob("*/*.json"):
                try:
                    if json.loads(path.read_text(encoding="utf-8")).get("agent_id") == agent_id:
                        return
                except (OSError, ValueError, AttributeError):
                    continue
        text = data.get("last_assistant_message")
    elif event == "PreToolUse" and data.get("tool_name") == "SubagentHandback":
        text = (data.get("tool_input") or {}).get("message")
    else:
        return
    try:
        if not isinstance(text, str):
            raise VerdictError("there is no report text")
        parsed = parse_verdict(text)
        if parsed["judge"] != judge:
            raise VerdictError(f"you are {judge}, but the verdict says JUDGE {parsed['judge']}")
        meta = read_json(block_review_dir(v, parsed["block"]) / "meta.json", None)
        if not meta or judge not in meta.get("judges", []):
            raise VerdictError(f"block {parsed['block']} was not sent to {judge} for review")
    except VerdictError as exc:
        reason = (f"Invalid verdict: {exc}. Reply again with only the verdict, in exactly the format of your instructions.")
        if event == "PreToolUse":
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                     "permissionDecisionReason": reason}}))
        elif not data.get("stop_hook_active"):
            print(json.dumps({"decision": "block", "reason": reason}))
        return
    write_json(block_review_dir(v, parsed["block"]) / f"{judge}.json", {"agent_id": agent_id, "text": text})


def cmd_capture_verdict(args):
    """PreToolUse (SubagentHandback) and SubagentStop hook. Whatever goes wrong must not reach the session."""
    try:
        capture_verdict(args)
    except Exception:
        pass


# -- Commands ----------------------------------------------------------------


def cmd_plan(args):
    v = open_vault(args)
    require_session(v)
    lines = [line.strip() for line in args.lines if line.strip()]
    if not lines:
        raise SrsError("Give the plan, one line per argument.")
    ensure_review_dir(v)
    write_text(review_dir(v) / "plan.md", "\n".join(lines) + "\n")
    print(f"Plan saved ({plural(len(lines), 'line')}):")
    print("\n".join(lines))


def review_context(v: Vault, block_id: str, file_arg):
    repo = require_repo(v)
    session = require_session(v)
    rel, block = find_block(v, block_id, file_arg)
    plan_path = review_dir(v) / "plan.md"
    if not plan_path.is_file():
        raise SrsError("No plan saved: run srs.py plan save with the approved plan first.", 2)
    plan = read_text(plan_path)
    facts = review_facts(v, repo, session, rel)
    return repo, rel, block, plan, facts, review_problems(v, facts, plan)


def review_prepare(v: Vault, args):
    repo, rel, block, plan, facts, problems = review_context(v, args.id, args.file)
    folder = block_review_dir(v, block.id)
    ensure_review_dir(v)
    shutil.rmtree(str(folder), ignore_errors=True)
    folder.mkdir(parents=True)
    judges = judges_for(facts)
    paths = write_review_inputs(v, repo, block, plan, facts, judges)
    write_json(folder / "meta.json", {"title": block.title, "source": rel, "judges": judges, "digest": facts["digest"],
                                      "checks": "ok" if not problems else "fail", "prepared": now_iso()})
    warning = unrecorded_warning(repo, load_session(v))
    if warning:
        print("\n".join(warning))
    print("\n".join(report_checks(problems)))
    if not problems:
        print(f"Judges: {', '.join(f'{JUDGE_PLUGIN}:{j}' for j in judges)}")
        for judge in judges:
            print(f"Input for {JUDGE_PLUGIN}:{judge}: {paths[judge].as_posix()}")


def review_verdict(v: Vault, args):
    meta = load_meta(v, args.id)
    verdicts, missing = {}, []
    for judge in meta["judges"]:
        parsed = stored_verdict(v, args.id, judge)
        if parsed is None:
            missing.append(judge)
            continue
        verdicts[judge] = parsed["verdict"]
        data = read_json(block_review_dir(v, args.id) / f"{judge}.json", {})
        print(f"## {judge}")
        print(data["text"].strip())
        print()
    if missing:
        print(f"Review: MISSING {', '.join(missing)}")
    else:
        print("Review: " + ("REVISE" if "REVISE" in verdicts.values() else "PASS"))


def review_final(v: Vault, args):
    meta = load_meta(v, args.id)
    folder = block_review_dir(v, args.id)
    if not args.note or not args.note.strip():
        raise SrsError("Give --note: one line on what changed, or why you disagree with the judge.")
    if (folder / "note.txt").exists():
        raise SrsError("The block was already revised once: park it (srs.py review park).", 2)
    verdicts = {j: stored_verdict(v, args.id, j) for j in meta["judges"]}
    if not all(verdicts.values()):
        raise SrsError("A judge's verdict is missing: park the block (srs.py review park).", 2)
    if not any(p["verdict"] == "REVISE" for p in verdicts.values()):
        raise SrsError("Every judge passed the block: there is nothing to revise. Commit it as it is.", 2)
    repo, rel, block, plan, facts, problems = review_context(v, args.id, args.file or meta["source"])
    for judge in judges_for(facts):
        if judge not in meta["judges"]:
            problems.append(f"the revision now needs {judge}, which did not review this block")
    write_text(folder / "note.txt", args.note.strip() + "\n")
    write_review_inputs(v, repo, block, plan, facts, meta["judges"])
    meta.update({"digest": facts["digest"], "checks": "ok" if not problems else "fail"})
    write_json(folder / "meta.json", meta)
    print("\n".join(report_checks(problems)))


def review_commit_body(v: Vault, repo: Repo, session: dict, block_id: str) -> str:
    """The lines a reviewed commit adds to its message; refuses unless the review is complete and current."""
    meta = load_meta(v, block_id)
    folder = block_review_dir(v, block_id)
    if meta.get("checks") != "ok":
        raise SrsError(f"The checks of block {block_id} failed: park it (srs.py review park).", 2)
    verdicts = {j: stored_verdict(v, block_id, j) for j in meta["judges"]}
    if not all(verdicts.values()):
        raise SrsError(f"A judge's verdict for block {block_id} is missing: park it (srs.py review park).", 2)
    revised = any(p["verdict"] == "REVISE" for p in verdicts.values())
    if revised and not (folder / "note.txt").exists():
        raise SrsError(f"A judge asked for changes to block {block_id}: revise it, then run srs.py review final.", 2)
    if review_facts(v, repo, session, meta["source"])["digest"] != meta["digest"]:
        raise SrsError(f"The pages changed after the last review step of block {block_id}: run srs.py review "
                       "prepare again to have the judges see them, or park the block.", 2)
    note = (folder / "note.txt").read_text(encoding="utf-8").strip() if (folder / "note.txt").exists() else ""
    lines = ["Reviewed by agents."]
    for judge, parsed in verdicts.items():
        line = f"{judge}: {parsed['verdict']}"
        if parsed["verdict"] == "REVISE":
            line += f" ({', '.join(parsed['failed'])}) — writer: {note}"
        lines.append(line)
    return "\n".join(lines)


def git_bytes(args: list, cwd: Path) -> bytes:
    proc = subprocess.run(["git"] + list(args), cwd=str(cwd), capture_output=True)
    if proc.returncode != 0:
        raise SrsError(f"git {' '.join(args)} failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


def review_park(v: Vault, args):
    repo = require_repo(v)
    session = require_session(v)
    if not args.reason or not args.reason.strip():
        raise SrsError("Give --reason: one line on why the block needs the user.")
    rel, block = find_block(v, args.id, args.file)
    files, has_patch = [], False
    ensure_review_dir(v)
    folder = block_review_dir(v, block.id)
    folder.mkdir(parents=True, exist_ok=True)
    if not args.waiting:
        changed = repo.status()
        files = [p for p in session.get("touched", [])
                 if p in changed and not p.startswith(SRS_DIR + "/") and p != rel]
        if files:
            new = [p for p in files if changed[p] == "??"]
            if new:
                git(["add", "-N", "--"] + new, v.root)  # so the patch includes files that are not tracked yet
            (folder / "proposal.patch").write_bytes(git_bytes(["diff", "HEAD", "--binary", "--"] + files, v.root))
            has_patch = True
        discard_touched(v, repo, session, include_state=False)
        save_session(v, session)
    entry = {"title": block.title, "source": rel, "reason": args.reason.strip(), "day": v.today.isoformat(),
             "waiting": bool(args.waiting), "files": files, "patch": has_patch}
    save_supervision(v, {**load_supervision(v), block.id: entry})
    print(f"Parked block {block.id} ({block.title}) for the user's supervision: {entry['reason']}")


def review_restore(v: Vault, args):
    repo = require_repo(v)
    require_session(v)
    entry = load_supervision(v).get(args.id)
    if entry is None:
        raise SrsError(f"Block {args.id} is not waiting for supervision.", 2)
    patch = block_review_dir(v, args.id) / "proposal.patch"
    if not entry.get("patch") or not patch.is_file():
        raise SrsError(f"No saved proposal for block {args.id}: write the block again.", 2)
    if git(["apply", "--check", str(patch)], v.root, check=False).returncode != 0:
        raise SrsError("Patch no longer applies: redo the block.", 2)
    git(["apply", str(patch)], v.root)
    for rel in entry.get("files", []):
        touch(v, rel)
    print(f"Restored the saved proposal for block {args.id}: {', '.join(entry.get('files', []))}")


def cmd_review(args):
    v = open_vault(args)
    {"prepare": review_prepare, "verdict": review_verdict, "final": review_final,
     "park": review_park, "restore": review_restore}[args.action](v, args)


# ---------------------------------------------------------------------------
# Context blocks: what each skill sees before Claude reads it
# ---------------------------------------------------------------------------


def rules_text() -> str:
    return read_text(RULES_FILE).strip() if RULES_FILE.exists() else "(rules.md missing)"


def context_header(v: Vault, command: str) -> list:
    return [
        f"# Recall context: /{command}",
        f"Vault: {v.root}",
        f"Study day: {v.today}",
        f"Talk to the user in: {v.config['language']}. Concept names and index descriptions in: "
        f"{v.config['concept_language']}.",
    ]


def session_lines(v: Vault) -> list:
    session = load_session(v)
    if session is None:
        return ["Session: none open."]
    try:
        return [interrupted_report(Repo(v.root), session)]
    except SrsError as exc:
        return [f"Session file present but git failed: {exc}"]


def annotated_index(text: str, idx: Index, state: dict) -> list:
    marks = {}
    for c in idx.concepts():
        name = find_state_name(state, c.name)
        if name:
            phase = state["concepts"][name]["phase"]
            marks[c.line] = "  [queued]" if phase == "new" else "  [review]"
    out = []
    for no, raw in enumerate(text.splitlines(), start=1):
        out.append(raw + marks.get(no, ""))
    return out


def render_due(v: Vault, state: dict, idx: Index) -> list:
    neighbors = index_neighbors(idx)
    items, info = daily_list(state, v.sched, v.today, neighbors)
    owners = idx.by_key()
    out = [
        f"Done today: {info['done']} of {v.sched['daily_limit']}. To ask now: {len(items)} "
        f"({info['learning']} learning, relearning or updated, {info['reviews']} reviews, {info['new']} new). "
        f"Carried over to tomorrow: {info['carried']}. New concepts still waiting: {info['new_waiting']}."
    ]
    if not items:
        nxt, count = next_due(state, v.sched, v.today, neighbors)
        if nxt:
            out.append(f"Nothing to ask today. Next: {nxt} ({count} concept{'s' if count != 1 else ''}).")
        elif state["concepts"]:
            out.append(f"Nothing to ask today, and nothing will come at this pace: {plural(len(state['concepts']), 'concept')} "
                       f"waiting, with new_per_day at {v.sched['new_per_day']} and the daily limit at "
                       f"{v.sched['daily_limit']}. Tell the user that /config changes the pace.")
        else:
            out.append("Nothing to ask today, and nothing is scheduled.")
        return out
    total = info["done"] + len(items)  # stays the same all day: each answer moves one from items to done
    out.append(f"Question count: start at {info['done'] + 1}/{total}, end at {total}/{total}.")
    if len(items) > PREPARE_ABOVE:
        first = PREPARE_FROM
        out.append(f"Prepare ahead: items {first} to {len(items)} of the list below "
                   f"(questions {info['done'] + first}/{total} to {total}/{total}).")
    out.append("")
    for n, name in enumerate(items, start=1):
        rec = state["concepts"][name]
        entries = owners.get(norm_key(name), [])
        page = entries[0].page if entries else None
        if page is None:
            where = "not in the index (orphan): skip it and tell the user to run /maintenance"
        elif page.status != "ok":
            where = f"[[{page.link}]] does not resolve ({page.status}): skip it and suggest /maintenance"
        else:
            where = page.path
        out.append(f"{n}. {name} — {describe_record(rec)}")
        out.append(f"   Page: {where}")
        if rec.get("focus"):
            out.append(f"   Focus (new content): {rec['focus']}")
        elif rec.get("gap"):
            out.append(f"   Gap (ask about this idea, not a new angle): {rec['gap']['idea']}")
        if rec.get("questions"):
            out.append("   Recent questions: " + " | ".join(rec["questions"]))
    return out


def context_setup(args) -> list:
    start = (Path(args.vault) if args.vault else Path.cwd()).resolve()
    root = find_vault(start)
    out = ["# Recall context: /setup"]
    if root is None:
        out.append(f"Folder: {start}")
        out.append("Recall: NOT CONFIGURED in this folder.")
        top = git_toplevel(start)
        if top:
            out.append(f"Git: repository at {top}" + ("" if git_has_head(start) else " (no commits yet)"))
            problem = git_identity_problem(start)
            if problem:
                out.append("Git identity: MISSING. " + problem.replace("\n", " "))
        else:
            out.append("Git: NOT A REPOSITORY. Recall needs one; init --git-init creates it (ask the user first).")
            out.append("Git identity: " + ("MISSING" if git_identity_problem(start) else "ok"))
        all_md = list_markdown(start)
        out.append(f"Markdown files, before exclusions and counting the inbox: {len(all_md)}. "
                   "After init, srs.py setup-batch gives the pages left to index.")
        out.append("")
        out.append("Defaults and what exists:")
        for key, default in DEFAULT_CONFIG["paths"].items():
            target = start / default
            exists = target.exists()
            line = f"  {key}: {default} — " + ("exists" if exists else {"new_pages": "created on first use",
                                                                          "folder_map": "drafted by /setup after init"}
                                                .get(key, "will be created"))
            if key == "inbox" and exists and target.is_file():
                counts = heading_counts(read_text(target))
                if counts:
                    line += "; headings found: " + ", ".join(f"{'#' * k} ×{c}" for k, c in sorted(counts.items()))
            out.append(line)
        conflicts = []
        for entry in sorted(os.listdir(str(start))):
            for key, default in DEFAULT_CONFIG["paths"].items():
                if entry.lower() in (default.lower(), Path(default).stem.lower()) and entry != default:
                    conflicts.append(f"  {entry} (similar to the default {key} '{default}')")
        if conflicts:
            out.append("Possible name conflicts:")
            out += sorted(set(conflicts))
        suggested = suggest_exclusions(start)
        out.append("Suggested exclusions: " + (", ".join(suggested) or "none detected"))
        themed = sorted({rel.split("/")[0] for rel in list_markdown(start)
                         if "/" in rel and not any(matches_pattern(rel, p) for p in suggested)})
        out.append(f"New page placement (init --new-page-placement; default {DEFAULT_CONFIG['new_page_placement']}):")
        out += [f"  {key}: {text}" for key, text in PLACEMENTS.items()]
        out.append("  Suggested: " + (f"folders (the vault has pages in folders such as {', '.join(themed[:3])})"
                                       if themed else "folders; with no folders yet, new pages go in the new-pages path"
                                       " until the vault has some"))
        out.append(f"Scheduling algorithms (init --algorithm; default {DEFAULT_CONFIG['scheduling']['algorithm']}):")
        out += [f"  {key}: {entry.summary}" for key, entry in ALGORITHMS.items()]
        out.append("")
        out.append("## Rules")
        out.append(rules_text())
        return out

    config = load_config(root)
    v = Vault(root=root, config=config, today=compute_today(config, args.today))
    out = context_header(v, "setup")
    out.append("Recall: configured.")
    out += session_lines(v)
    idx, _ = load_index(v)
    indexed = {p.path for p in idx.pages if p.path}
    pages = content_pages(v)
    left = [p for p in pages if p not in indexed]
    out.append(f"Content pages: {len(pages)}; in the index: {len(pages) - len(left)}; not yet indexed: {len(left)}.")
    batch = left[: v.limits["setup_batch"]]
    if batch:
        out.append(f"Next batch to index ({len(batch)}):")
        out += [f"  {rel}" for rel in batch]
    else:
        out.append("Every content page is indexed.")
    out.append(f"Index file: {v.index_rel} ({len(idx.pages)} pages, {sum(1 for _ in idx.concepts())} concepts)")
    out.append(f"Description limit: {v.limits['description_max_chars']} characters.")
    out += folder_map_setup_lines(v)
    out.append("")
    out.append("## Rules")
    out.append(rules_text())
    return out


def folder_map_setup_lines(v: Vault) -> list:
    """What /setup needs to draft the folder map: nothing when placement is flat,
    one line when the map is complete; otherwise the folders to map, with their
    page names, and the template when the map does not exist yet."""
    if v.placement != "folders":
        return []
    exists = (v.root / v.folder_map_rel).is_file()
    missing = folder_map_checks(v)["unmapped_folders"]
    if exists and not missing:
        return [f"Folder map: {v.folder_map_rel} (every folder with pages has a row)."]
    if not missing:
        return [f"Folder map: none yet ({v.folder_map_rel}); no folder has pages, so there is nothing to map."]
    out = [""]
    if exists:
        out.append(f"## Folder map: {v.folder_map_rel} has no row for {plural(len(missing), 'folder')}")
    else:
        out.append(f"## Folder map: {v.folder_map_rel} DOES NOT EXIST")
    pages = content_pages(v)
    for folder in missing:
        names = [p[len(folder) + 1:-3] for p in pages if p.startswith(folder + "/")]
        out.append(f"  {folder}/ — {plural(len(names), 'page')}: " + "; ".join(names[:15])
                   + ("; ..." if len(names) > 15 else ""))
    if not exists:
        out.append("")
        out.append("Template:")
        out.append(read_text(TEMPLATES / "folders.md").rstrip("\n"))
    return out


def suggest_exclusions(root: Path) -> list:
    found = []
    obsidian = root / ".obsidian"
    for rel, key in (("templates.json", "folder"), ("daily-notes.json", "folder"),
                     ("plugins/templater-obsidian/data.json", "templates_folder"),
                     ("plugins/obsidian-excalidraw-plugin/data.json", "folder")):
        try:
            value = json.loads((obsidian / rel).read_text(encoding="utf-8")).get(key)
        except (OSError, ValueError, AttributeError):
            value = None
        if value and isinstance(value, str) and (root / value).is_dir():
            found.append(norm_rel(value) + "/")
    for entry in sorted(os.listdir(str(root))):
        if (root / entry).is_dir() and not entry.startswith(".") and any(h in entry.lower() for h in EXCLUSION_HINTS):
            found.append(entry + "/")
    if any(rel.lower().endswith(".excalidraw.md") for rel in list_markdown(root)):
        found.append("*.excalidraw.md")
    unique = []
    for item in found:
        if item.lower() not in [u.lower() for u in unique]:
            unique.append(item)
    return unique


def context_integrate(v: Vault, extra: list) -> list:
    state = load_state(v)
    idx, text = load_index(v)
    limit, source = None, None
    for arg in extra:
        if arg.isdigit():
            limit = int(arg)
        elif arg.strip():
            source = arg.strip()
    out = context_header(v, "integrate")
    out += session_lines(v)
    out.append(queue_summary(state, v))
    out.append(f"Review mode: {v.integrate_review}")
    out += review_prompt_lines(v)
    rel, path = block_source(v, source)
    out.append("")
    if not path.exists():
        out.append(f"## Source: {rel} — FILE NOT FOUND")
    else:
        blocks, _ = parse_blocks(read_text(path), v.config["inbox_heading_level"])
        kind = "inbox" if rel == v.inbox_rel else "file (pass --file to the inbox commands)"
        out.append(f"## Source: {rel} ({kind}) — {len(blocks)} block(s), newest first")
        out.append(f"Process up to: {limit if limit else 'all'}")
        for b in reversed(blocks):
            out.append(f"  [{b.id}] {b.title} ({len(b.text)} characters)")
        if not blocks:
            level = v.config["inbox_heading_level"]
            out.append(f"  (no blocks: a block starts with a heading of level {level}, '{'#' * level} Title')")
        waiting = {b: e for b, e in live_supervision(v).items() if (e.get("source") or v.inbox_rel) == rel}
        if waiting:
            out.append("")
            out.append("## Blocks waiting for supervision (an earlier agent-review round parked them; they are still in the source)")
            for block_id, e in waiting.items():
                out.append(f"  [{block_id}] {e.get('title', '')} — {e.get('reason', '')} (parked {e.get('day', '?')})")
    out.append("")
    out.append(f"## Index ({v.index_rel}) — markers: [review] in review, [queued] waiting; unmarked = index only")
    out += annotated_index(text, idx, state) if text.strip() else ["(empty)"]
    out.append("")
    if v.placement == "flat":
        out.append(f"New pages go in: {v.new_pages_rel}/ (placement: flat; ignore the vault's folders)")
    else:
        out.append("New pages (placement: folders): put each in the existing folder whose topic fits best. "
                   "If none fits, propose a new folder named by the vault's convention. "
                   f"Use {v.new_pages_rel}/ only if the vault has no thematic folders.")
        outline = folder_outline(v)
        root_pages = sum(1 for page in content_pages(v) if "/" not in page)
        out.append("")
        out.append("## Folders (two levels; hidden and excluded left out; pages counted include subfolders)")
        if root_pages:
            out.append(f"  (vault root): {plural(root_pages, 'page')}")
        out += [f"  {'  ' * rel.count('/')}{rel.rsplit('/', 1)[-1]}/ — {plural(n, 'page')}" for rel, n in outline]
        if not outline:
            out.append("  (no folders)")
        out.append("")
        map_path = v.root / v.folder_map_rel
        if map_path.is_file():
            out.append(f"## Folder map ({v.folder_map_rel}): follow it to place new pages and propose new folders")
            out.append(read_text(map_path).rstrip("\n"))
        else:
            out.append(f"## Folder map: none ({v.folder_map_rel} does not exist; /setup drafts it). "
                       "Pick folders by their names.")
    out.append("")
    out.append(f"Description limit: {v.limits['description_max_chars']} characters.")
    out.append("")
    out.append("## Rules")
    out.append(rules_text())
    return out


def context_quiz(v: Vault) -> list:
    state = load_state(v)
    idx, _ = load_index(v)
    out = context_header(v, "quiz")
    out += session_lines(v)
    out.append("")
    out.append("## Today's list")
    out += render_due(v, state, idx)
    out.append("")
    out.append("## Quiz rules (.srs/quiz-rules.md)")
    rules_path = v.srs / "quiz-rules.md"
    out.append(read_text(rules_path).strip() if rules_path.exists() else read_text(TEMPLATES / "quiz-rules.md").strip())
    out.append("")
    out.append("## Rules")
    out.append(rules_text())
    return out


def recent_integrated_pages(v: Vault, window: int = 30) -> list:
    proc = git(["log", f"--since={window}.days", "--relative", "--name-only", "--format=%x00%s", "--", "."],
               v.root, check=False)
    if proc.returncode != 0:
        return []
    pages = []
    for chunk in proc.stdout.split("\0"):
        lines = [line for line in chunk.splitlines() if line.strip()]
        if not lines or not lines[0].startswith("integrate:"):
            continue
        for rel in lines[1:]:
            rel = norm_rel(rel)
            if rel.lower().endswith(".md") and not is_system_file(v, rel) and rel not in pages:
                pages.append(rel)
    return pages


def context_promote(v: Vault, extra: list) -> list:
    state = load_state(v)
    idx, _ = load_index(v)
    query = " ".join(a for a in extra if a.strip()).strip()
    out = context_header(v, "promote")
    out += session_lines(v)
    out.append(queue_summary(state, v))
    candidates = defaultdict(list)
    for c in idx.concepts():
        if find_state_name(state, c.name):
            continue
        where = c.page.path or f"[[{c.page.link}]]"
        if query:
            hay = " ".join([where, c.page.link, c.name, c.desc or ""]).casefold()
            if query.casefold() not in hay:
                continue
        candidates[where].append(c)
    total = sum(len(v_) for v_ in candidates.values())
    out.append("")
    if query:
        out.append(f"## Candidates matching \"{query}\" (in the index, not in review): {total}")
        order = sorted(candidates)
    else:
        recent = recent_integrated_pages(v)
        out.append(f"## No query. Pages touched by /integrate in the last 30 days: {len(recent)}")
        out += [f"  {p}" for p in recent[:30]]
        out.append(f"## Candidates (in the index, not in review): {total}; pages above first")
        order = [p for p in recent if p in candidates] + sorted(p for p in candidates if p not in recent)
    shown = 0
    for where in order:
        if shown >= 200:
            out.append(f"... and {total - shown} more; narrow the query.")
            break
        out.append(f"### {where}")
        for c in candidates[where]:
            out.append(f"- {c.name}" + (f"{DESC_SEP}{c.desc}" if c.desc else ""))
            shown += 1
    out.append("")
    out.append("## Rules")
    out.append(rules_text())
    return out


def context_config(v: Vault) -> list:
    out = context_header(v, "config")
    out += session_lines(v)
    out.append("")
    out.append("## Current pace")
    out += pace_lines(v)
    out.append(review_mode_line(v))
    out.append("")
    out.append("## Rules")
    out.append(rules_text())
    return out


def context_maintenance(v: Vault) -> list:
    report = run_checks(v)
    out = context_header(v, "maintenance")
    out += session_lines(v)
    out.append("")
    out.append("## Deterministic checks")
    out += format_checks(v, report)
    over = {p for p, _ in report["over_size"]}
    dup_pages = set()
    for entries in report["index"].by_key().values():
        if len(entries) > 1:
            dup_pages.update(e.page.path for e in entries if e.page.path)
    batch = [p for p in report["unverified"] if p.path not in over and p.path not in dup_pages]
    batch = batch[: v.limits["maintenance_batch"]]
    out.append("")
    out.append(f"## Batch to audit ({len(batch)} pages; over-size pages and pages with shared names are left out)")
    out.append("Each page shows its vault path, then its absolute path in backticks. Open pages by the absolute path.")
    for page in batch:
        out.append(f"### {page.path}")
        out.append(f"Absolute path: `{(v.root / page.path).as_posix()}`")
        out += [f"    {line}" for line in page.entry_text().splitlines()]
    out.append("")
    out.append(f"Full index: {v.index_rel}")
    out.append("")
    out.append("## Rules")
    out.append(rules_text())
    return out


def cmd_context(args):
    command = args.command
    try:
        if command == "setup":
            lines = context_setup(args)
        else:
            try:
                v = open_vault(args)
            except SrsError as exc:
                lines = [f"# Recall context: /{command}", f"NOT SET UP: {exc}"]
            else:
                if command == "integrate":
                    lines = context_integrate(v, args.extra)
                elif command == "quiz":
                    lines = context_quiz(v)
                elif command == "promote":
                    lines = context_promote(v, args.extra)
                elif command == "config":
                    lines = context_config(v)
                else:
                    lines = context_maintenance(v)
    except SrsError as exc:
        lines = [f"# Recall context: /{command}", f"ERROR: {exc}"]
    print("\n".join(lines))


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="srs.py", description="Recall engine: scheduling, checks and git sessions.")
    parser.add_argument("--vault", help="vault folder (default: found from the current folder)")
    parser.add_argument("--today", help="study day override, YYYY-MM-DD (testing)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("version", help="print the version").set_defaults(func=cmd_version)

    p = sub.add_parser("init", help="set up Recall in a vault")
    p.add_argument("--algorithm", choices=list(ALGORITHMS), help="scheduling algorithm (default: anki)")
    p.add_argument("--language")
    p.add_argument("--concept-language")
    p.add_argument("--inbox")
    p.add_argument("--index")
    p.add_argument("--new-pages")
    p.add_argument("--folder-map")
    p.add_argument("--new-page-placement", choices=list(PLACEMENTS),
                   help="where /integrate puts new pages (default: folders)")
    p.add_argument("--heading-level", type=int)
    p.add_argument("--exclude", action="append")
    p.add_argument("--git-init", action="store_true", help="create a git repository if the folder has none")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("context", help="print the context block a command starts from")
    p.add_argument("command", choices=["setup", "integrate", "quiz", "promote", "maintenance", "config"])
    p.add_argument("extra", nargs="*")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("begin", help="open a session (commits manual edits first)")
    p.add_argument("command")
    p.set_defaults(func=cmd_begin)
    sub.add_parser("diff", help="show the session's uncommitted changes").set_defaults(func=cmd_diff)
    p = sub.add_parser("commit", help="commit what the session touched")
    p.add_argument("-m", "--message", required=True)
    p.add_argument("--review", metavar="ID", help="commit a block that the agent review cleared (adds its verdicts to the message)")
    p.set_defaults(func=cmd_commit)
    sub.add_parser("discard", help="undo the session's uncommitted note edits").set_defaults(func=cmd_discard)
    sub.add_parser("end", help="close the session").set_defaults(func=cmd_end)
    sub.add_parser("abort", help="undo everything uncommitted from the session and close it").set_defaults(func=cmd_abort)
    sub.add_parser("status", help="session and git status").set_defaults(func=cmd_status)

    sub.add_parser("due", help="today's quiz list").set_defaults(func=cmd_due)
    p = sub.add_parser("record", help="record an answer")
    p.add_argument("concept")
    p.add_argument("result", choices=["pass", "fail"])
    p.add_argument("--question", help="the question asked, in a few plain words")
    p.add_argument("--gap", help="on a fail, the idea the answer missed, in a few plain words")
    p.set_defaults(func=cmd_record)
    p = sub.add_parser("amend", help="correct today's answer for a concept (a contested verdict)")
    p.add_argument("concept")
    p.add_argument("result", choices=["pass", "fail"])
    p.add_argument("--gap", help="when amending to fail, the idea the answer missed")
    p.set_defaults(func=cmd_amend)
    p = sub.add_parser("gap", help="open a concept's gap by hand, or close it")
    p.add_argument("concept")
    p.add_argument("idea", nargs="?")
    p.add_argument("--close", action="store_true")
    p.set_defaults(func=cmd_gap)
    sub.add_parser("today", help="today's score").set_defaults(func=cmd_today)
    p = sub.add_parser("add", help="queue concepts for review")
    p.add_argument("--source", choices=["integrate", "promote"], required=True)
    p.add_argument("concepts", nargs="+")
    p.set_defaults(func=cmd_add)
    p = sub.add_parser("update", help="mark new content for a concept in review")
    p.add_argument("concept")
    p.add_argument("--focus", required=True)
    p.set_defaults(func=cmd_update)
    p = sub.add_parser("rename", help="rename a concept in the schedule (after renaming it in the index)")
    p.add_argument("old")
    p.add_argument("new")
    p.set_defaults(func=cmd_rename)
    p = sub.add_parser("remove", help="remove a concept from the schedule")
    p.add_argument("concept")
    p.set_defaults(func=cmd_remove)
    p = sub.add_parser("flag", help="flag a page as too thin for good questions, or a concept as misplaced on it")
    p.add_argument("page")
    p.add_argument("--misplaced", metavar="CONCEPT", help="flag this concept as defined on the wrong page")
    p.add_argument("--to", help="with --misplaced: where the concept belongs (a page or a subject)")
    p.set_defaults(func=cmd_flag)
    p = sub.add_parser("unflag", help="remove a thin flag, or a misplaced flag")
    p.add_argument("page")
    p.add_argument("--misplaced", metavar="CONCEPT", help="remove the misplaced flag from this concept instead")
    p.set_defaults(func=cmd_unflag)
    sub.add_parser("stats", help="retention, counts and queue").set_defaults(func=cmd_stats)
    p = sub.add_parser("config", help="show or change the daily pace (commits the change itself)")
    p.add_argument("--new-per-day", type=int, help="new concepts per day (0 pauses them)")
    p.add_argument("--daily-limit", type=int, help="concepts per day, reviews and new together")
    p.add_argument("--integrate-review", choices=REVIEW_MODES, help="who reviews /integrate: you (human) or judge agents (agent)")
    p.add_argument("--remind-later", action="store_true", help="postpone the reminder to try the agent review")
    p.set_defaults(func=cmd_config)

    sub.add_parser("check", help="deterministic maintenance checks").set_defaults(func=cmd_check)
    p = sub.add_parser("verify", help="seal pages that passed the audit")
    p.add_argument("pages", nargs="+")
    p.set_defaults(func=cmd_verify)
    p = sub.add_parser("unseal", help="remove seals, e.g. from the other page of a duplicate")
    p.add_argument("pages", nargs="+")
    p.set_defaults(func=cmd_unseal)
    sub.add_parser("prune-seals", help="remove seals of files that are gone or no longer pages").set_defaults(
        func=cmd_prune_seals)
    p = sub.add_parser("inbox", help="list, show or remove inbox blocks")
    p.add_argument("action", choices=["list", "show", "remove"])
    p.add_argument("ids", nargs="*")
    p.add_argument("--file", help="another file with blocks, instead of the inbox")
    p.set_defaults(func=cmd_inbox)
    sub.add_parser("setup-batch", help="the next pages to index").set_defaults(func=cmd_setup_batch)
    sub.add_parser("welcome", help="session-start status as hook JSON (read-only)").set_defaults(func=cmd_welcome)
    p = sub.add_parser("plan", help="save the approved /integrate plan for the agent review")
    p.add_argument("action", choices=["save"])
    p.add_argument("lines", nargs="+")
    p.set_defaults(func=cmd_plan)
    p = sub.add_parser("review", help="agent review of one block: prepare, verdict, final, park, restore")
    p.add_argument("action", choices=["prepare", "verdict", "final", "park", "restore"])
    p.add_argument("id")
    p.add_argument("--file", help="the file the block comes from, when it is not the inbox")
    p.add_argument("--note", help="final: one line on what the writer changed, or why it disagrees")
    p.add_argument("--reason", help="park: one line on why the block needs the user")
    p.add_argument("--waiting", action="store_true", help="park: the block was not written, it waits for another")
    p.set_defaults(func=cmd_review)
    sub.add_parser("capture-verdict", help="hook: save a judge agent's verdict").set_defaults(func=cmd_capture_verdict)
    return parser


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except SrsError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    return 0


if __name__ == "__main__":
    sys.exit(main())
