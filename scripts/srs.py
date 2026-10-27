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
import csv
import datetime as dt
import fnmatch
import hashlib
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

VERSION = "0.3.0"
SRS_DIR = ".srs"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = PLUGIN_ROOT / "templates"
RULES_FILE = PLUGIN_ROOT / "rules.md"

SYSTEM_FILE_NAMES = {"claude.md", "claude.local.md", "agents.md"}
GITIGNORE_LINES = [".srs/session.json", ".obsidian/workspace*.json", ".trash/"]
SESSION_EXCLUDE = ":(exclude).srs/session.json"
LOG_HEADER = ["day", "concept", "result", "phase"]
DESC_SEP = " — "  # space, em dash, space
SOURCE_RANK = {"integrate": 0, "promote": 1}
EXCLUSION_HINTS = ("template", "attachment", "asset", "excalidraw", "daily", "journal", "archive")
PHASES = ("new", "learning", "review", "relearning")

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
    "paths": {"inbox": "inbox.md", "index": "index.md", "new_pages": "wiki"},
    "new_page_placement": "folders",
    "inbox_heading_level": 2,
    "exclude": [],
    "scheduling": {
        "algorithm": "anki",
        "daily_limit": 20,
        "new_per_day": 5,
        "day_starts_at": 4,
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
    return rel in (v.inbox_rel, v.index_rel) or rel.rsplit("/", 1)[-1].lower() in SYSTEM_FILE_NAMES


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


def resolve_links(v: Vault, idx: Index) -> None:
    """Resolve [[links]] the way Obsidian does: by file name when unique, by path otherwise."""
    all_md = list_markdown(v.root)
    by_stem = defaultdict(list)
    by_path = {}
    for rel in all_md:
        by_stem[Path(rel).stem.casefold()].append(rel)
        by_path[rel[:-3].casefold()] = rel
    for page in idx.pages:
        link = page.link.strip().replace("\\", "/")
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
        page.candidates = sorted(cands)
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
    return {"version": 1, "next_order": 1, "concepts": {}, "flags": {"thin": {}}}


def load_state(v: Vault) -> dict:
    state = read_json(v.srs / "state.json", empty_state())
    state.setdefault("concepts", {})
    state.setdefault("next_order", 1)
    state.setdefault("flags", {}).setdefault("thin", {})
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
    """thirds: spread an interval by up to +/- the fuzz share, deterministically."""
    if interval < params["fuzz_min_interval"] or params["fuzz"] <= 0:
        return interval
    r = unit_random(name, day) * 2 - 1
    return max(params["min_interval"], interval + round_half_up(interval * params["fuzz"] * r))


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


def daily_list(state: dict, sched: dict, today: dt.date):
    """Today's quiz list and a summary. Learning, relearning and updated concepts
    first, then reviews (most overdue first, shorter interval first), then new
    concepts in the slots left, up to the daily limit."""
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
    items += waiting_new[:slots]
    info = {
        "done": len(done),
        "capacity": capacity,
        "carried": carried,
        "learning": len([n for n in items if first(concepts[n])]),
        "reviews": len([n for n in items if concepts[n]["phase"] == "review" and not first(concepts[n])]),
        "new": len([n for n in items if concepts[n]["phase"] == "new"]),
        "new_waiting": len(waiting_new) - min(slots, len(waiting_new)),
    }
    return items, info


def next_due(state: dict, today: dt.date):
    """The next study day with something due, and how many concepts then."""
    dates = []
    for rec in state["concepts"].values():
        if rec["phase"] == "new":
            queued = parse_day(rec.get("queued"))
            if queued:
                dates.append(max(queued + days(1), today + days(1)))
        else:
            due = parse_day(rec.get("due"))
            if due:
                dates.append(max(due, today + days(1)))
    if not dates:
        return None, 0
    first = min(dates)
    return first, sum(1 for d in dates if d == first)


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
        args += pathspecs or [".", SESSION_EXCLUDE]
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
        git(["add", "-A", "--", ".", SESSION_EXCLUDE], self.root)
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


# ---------------------------------------------------------------------------
# Checks (the deterministic half of /maintenance)
# ---------------------------------------------------------------------------


def run_checks(v: Vault) -> dict:
    idx, _ = load_index(v)
    state = load_state(v)
    verified = load_verified(v)
    limits = v.limits
    pages = content_pages(v)
    indexed = {p.path for p in idx.pages if p.path}
    report = {
        "over_size": [],
        "duplicates": [],
        "long_descriptions": [],
        "link_problems": [],
        "index_problems": idx.problems,
        "orphans": [],
        "unindexed": [p for p in pages if p not in indexed],
        "unverified": [],
        "thin": sorted(state["flags"]["thin"].items()),
        "leeches": [],
        "index": idx,
    }
    dup_pages = {e.page.path for entries in idx.by_key().values() if len(entries) > 1 for e in entries}
    for page in idx.pages:
        if page.status == "ok":
            chars = len(read_text(v.root / page.path))
            if chars > limits["page_max_chars"]:
                report["over_size"].append((page.path, chars))
            # a page that shares a concept name stays unsealed until the duplicate is fixed
            if not seal_ok(v, verified, page) or page.path in dup_pages:
                report["unverified"].append(page)
        else:
            report["link_problems"].append(page)
        for c in page.concepts:
            if c.desc and len(c.desc) > limits["description_max_chars"]:
                report["long_descriptions"].append((c.name, len(c.desc)))
    for key, entries in sorted(idx.by_key().items()):
        if len(entries) > 1:
            report["duplicates"].append((entries[0].name, [f"[[{e.page.link}]]" for e in entries]))
    owners = idx.by_key()
    for name, rec in sorted(state["concepts"].items()):
        if norm_key(name) not in owners:
            report["orphans"].append(name)
        if int(rec.get("lapses") or 0) >= v.leech_at:
            report["leeches"].append((name, rec["lapses"]))
    return report


def format_checks(v: Vault, report: dict) -> list:
    out = []
    limits = v.limits

    def section(title, items, render):
        out.append(f"{title}: {len(items) if items else 'none'}")
        for item in items:
            out.append(f"  - {render(item)}")

    section(f"Pages over {limits['page_max_chars']} characters", report["over_size"],
            lambda x: f"{x[0]} ({x[1]} characters)")
    section("Concept names used on more than one page", report["duplicates"],
            lambda x: f"{x[0]}: {', '.join(x[1])}")
    section(f"Descriptions over {limits['description_max_chars']} characters", report["long_descriptions"],
            lambda x: f"{x[0]} ({x[1]} characters)")

    def link_line(page):
        if page.status == "missing":
            return f"[[{page.link}]] (index line {page.line}): no such page"
        if page.status == "ambiguous":
            return f"[[{page.link}]] (index line {page.line}): ambiguous, use the path: {', '.join(page.candidates)}"
        return f"[[{page.link}]] (index line {page.line}): points to an excluded file ({page.path})"

    section("Index links that don't resolve", report["link_problems"], link_line)
    section("Index lines not understood", report["index_problems"], lambda x: f"line {x[0]}: {x[1]}")
    section("Concepts in review but not in the index (orphans)", report["orphans"], lambda x: x)
    section("Pages not in the index (index them with /setup)", report["unindexed"], lambda x: x)
    section("Indexed pages without a valid seal", report["unverified"], lambda p: p.path)
    section("Pages flagged as thin by /quiz", report["thin"], lambda x: f"{x[0]} (flagged {x[1]})")
    section(f"Leeches ({v.leech_at}+ lapses)", report["leeches"], lambda x: f"{x[0]} ({x[1]} lapses)")
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
    save_session(v, {"command": args.command, "started": now_iso(), "touched": []})
    note = f" Committed your manual edits first ({plural(snapped, 'file')}, vault: manual edits)." if snapped else ""
    print(f"Session open for /{args.command}.{note}")


def cmd_diff(args):
    v = open_vault(args)
    repo = require_repo(v)
    session = require_session(v)
    changed = repo.status()
    paths = [p for p in session.get("touched", []) if p in changed]
    if not paths:
        print("No changes to review.")
        return
    content = [p for p in paths if not p.startswith(SRS_DIR + "/")]
    state_files = [p for p in paths if p.startswith(SRS_DIR + "/")]
    tracked = [p for p in content if changed[p] != "??"]
    untracked = [p for p in content if changed[p] == "??"]
    chunks = []
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
    print("\n".join(chunks).strip("\n"))


def cmd_commit(args):
    v = open_vault(args)
    repo = require_repo(v)
    session = require_session(v)
    paths = pending_paths(repo, session)
    session["touched"] = []
    if not paths:
        save_session(v, session)
        print("Nothing to commit.")
        return
    sha = repo.commit(paths, args.message)
    save_session(v, session)
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
    print(f"Session for /{session.get('command')} closed.")


def cmd_abort(args):
    v = open_vault(args)
    session = load_session(v)
    if session is None:
        print("No session is open.")
        return
    repo = require_repo(v)
    restored, removed, folders = discard_touched(v, repo, session, include_state=True)
    session_path(v).unlink()
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


def cmd_record(args):
    v = open_vault(args)
    state = load_state(v)
    name = find_state_name(state, args.concept)
    if name is None:
        raise SrsError(f"'{args.concept}' is not in the review schedule.", 2)
    rec = state["concepts"][name]
    if rec.get("last") == v.today.isoformat():
        raise SrsError(f"{name} was already answered today; repeats in the same session don't count.", 2)
    items, _ = daily_list(state, v.sched, v.today)
    if name not in items:
        raise SrsError(f"{name} is not in today's list.", 2)
    before = rec["phase"]
    updated = apply_answer(rec, args.result == "pass", v.today, v.sched, name)
    if args.question:
        updated["questions"] = (list(rec.get("questions") or []) + [short(args.question, 200)])[-3:]
    state["concepts"][name] = updated
    save_state(v, state)
    append_log(v, [v.today.isoformat(), name, args.result, before])
    print(describe_answer(name, before, updated, v.today, v.leech_at))


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
    state["flags"]["thin"][rel] = v.today.isoformat()
    save_state(v, state)
    print(f"Flagged {rel} as thin; /maintenance will report it.")


def cmd_unflag(args):
    v = open_vault(args)
    state = load_state(v)
    rel = norm_rel(args.page)
    if state["flags"]["thin"].pop(rel, None) is None:
        print(f"{rel} was not flagged.")
        return
    save_state(v, state)
    print(f"Removed the thin flag from {rel}.")


def cmd_stats(args):
    v = open_vault(args)
    state = load_state(v)
    rows = read_log(v)
    start = v.today - days(29)
    recent = [r for r in rows if parse_day(r["day"]) >= start]
    reviews = [r for r in recent if r["phase"] == "review"]
    name, _, entry = algorithm(v.sched)
    knob = f"scheduling.{name}.{entry.growth_key}"
    print(f"Study day: {v.today}")
    print(f"Scheduling algorithm: {name}")
    if reviews:
        rate = sum(1 for r in reviews if r["result"] == "pass") / len(reviews)
        low, high = v.config["target_retention"]
        if len(reviews) < 30:
            verdict = "too few reviews to judge; wait for at least 30"
        elif rate > 0.95:
            verdict = f"above 95%: reviews come too early; consider raising {knob}"
        elif rate < 0.80:
            verdict = f"below 80%: reviews come too late; consider lowering {knob}"
        elif low <= rate <= high:
            verdict = "on target"
        else:
            verdict = f"outside the {low:.0%}-{high:.0%} target, but close"
        print(f"Retention on reviews, last 30 days: {rate:.0%} of {len(reviews)} ({verdict})")
    else:
        print("Retention on reviews, last 30 days: no reviews yet")
    counts = defaultdict(int)
    for rec in state["concepts"].values():
        counts[rec["phase"]] += 1
    print("Concepts: " + ", ".join(f"{p} {counts[p]}" for p in ("new", "learning", "review", "relearning")))
    print(queue_summary(state, v))
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
    items, info = daily_list(state, v.sched, v.today)
    owners = idx.by_key()
    out = [
        f"Done today: {info['done']} of {v.sched['daily_limit']}. To ask now: {len(items)} "
        f"({info['learning']} learning, relearning or updated, {info['reviews']} reviews, {info['new']} new). "
        f"Carried over to tomorrow: {info['carried']}. New concepts still waiting: {info['new_waiting']}."
    ]
    if not items:
        nxt, count = next_due(state, v.today)
        out.append(f"Nothing to ask today. Next: {nxt} ({count} concept{'s' if count != 1 else ''})."
                   if nxt else "Nothing to ask today, and nothing is scheduled.")
        return out
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
            line = f"  {key}: {default} — {'exists' if exists else 'will be created' if key != 'new_pages' else 'created on first use'}"
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
    out.append("")
    out.append("## Rules")
    out.append(rules_text())
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
    for page in batch:
        out.append(f"- {page.path}")
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
    p.add_argument("--new-page-placement", choices=list(PLACEMENTS),
                   help="where /integrate puts new pages (default: folders)")
    p.add_argument("--heading-level", type=int)
    p.add_argument("--exclude", action="append")
    p.add_argument("--git-init", action="store_true", help="create a git repository if the folder has none")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("context", help="print the context block a command starts from")
    p.add_argument("command", choices=["setup", "integrate", "quiz", "promote", "maintenance"])
    p.add_argument("extra", nargs="*")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("begin", help="open a session (commits manual edits first)")
    p.add_argument("command")
    p.set_defaults(func=cmd_begin)
    sub.add_parser("diff", help="show the session's uncommitted changes").set_defaults(func=cmd_diff)
    p = sub.add_parser("commit", help="commit what the session touched")
    p.add_argument("-m", "--message", required=True)
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
    p.set_defaults(func=cmd_record)
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
    p = sub.add_parser("flag", help="flag a page as too thin for good questions")
    p.add_argument("page")
    p.set_defaults(func=cmd_flag)
    p = sub.add_parser("unflag", help="remove a thin flag")
    p.add_argument("page")
    p.set_defaults(func=cmd_unflag)
    sub.add_parser("stats", help="retention, counts and queue").set_defaults(func=cmd_stats)

    sub.add_parser("check", help="deterministic maintenance checks").set_defaults(func=cmd_check)
    p = sub.add_parser("verify", help="seal pages that passed the audit")
    p.add_argument("pages", nargs="+")
    p.set_defaults(func=cmd_verify)
    p = sub.add_parser("unseal", help="remove seals, e.g. from the other page of a duplicate")
    p.add_argument("pages", nargs="+")
    p.set_defaults(func=cmd_unseal)
    p = sub.add_parser("inbox", help="list, show or remove inbox blocks")
    p.add_argument("action", choices=["list", "show", "remove"])
    p.add_argument("ids", nargs="*")
    p.add_argument("--file", help="another file with blocks, instead of the inbox")
    p.set_defaults(func=cmd_inbox)
    sub.add_parser("setup-batch", help="the next pages to index").set_defaults(func=cmd_setup_batch)
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
