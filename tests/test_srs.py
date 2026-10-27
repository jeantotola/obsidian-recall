"""Tests for Recall's engine (scripts/srs.py) and hook (hooks/guard.py).

Run from the repository root:  python3 -m unittest discover -s tests -v
"""

import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRS = ROOT / "scripts" / "srs.py"
GUARD = ROOT / "hooks" / "guard.py"

spec = importlib.util.spec_from_file_location("srs", SRS)
srs = importlib.util.module_from_spec(spec)
sys.modules["srs"] = srs
spec.loader.exec_module(srs)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}
D = dt.date.fromisoformat


def sched(algorithm="thirds", **overrides):
    """A scheduling config for one algorithm. Shared keys (daily_limit and so on) go
    at the top level; any other key goes into the algorithm's parameters."""
    base = json.loads(json.dumps(srs.DEFAULT_CONFIG["scheduling"]))
    base["algorithm"] = algorithm
    for key, value in overrides.items():
        if key in base and not isinstance(base[key], dict):
            base[key] = value
        else:
            base[algorithm][key] = value
    return base


class ThirdsTest(unittest.TestCase):
    def setUp(self):
        self.s = sched("thirds", fuzz=0)

    def answer(self, rec, passed, day):
        return srs.apply_answer(rec, passed, D(day), self.s, "X")

    def test_new_pass_graduates_with_interval_one(self):
        rec = self.answer(srs.new_record("integrate", D("2026-01-01"), 1), True, "2026-01-02")
        self.assertEqual((rec["phase"], rec["interval"], rec["due"]), ("review", 1, "2026-01-03"))
        self.assertEqual(rec["introduced"], "2026-01-02")
        self.assertNotIn("ease", rec)

    def test_new_fail_goes_to_learning_tomorrow(self):
        rec = self.answer(srs.new_record("integrate", D("2026-01-01"), 1), False, "2026-01-02")
        self.assertEqual((rec["phase"], rec["due"]), ("learning", "2026-01-03"))

    def test_always_right_sequence(self):
        rec = srs.new_record("integrate", D("2026-01-01"), 1)
        day = D("2026-01-02")
        seen = []
        for _ in range(7):
            rec = srs.apply_answer(rec, True, day, self.s, "X")
            seen.append(rec["interval"])
            day = D(rec["due"])
        self.assertEqual(seen, [1, 3, 8, 20, 50, 125, 313])

    def test_lapse_cuts_to_a_third_after_a_pass(self):
        rec = {"phase": "review", "interval": 90, "last": "2026-01-01", "due": "2026-04-01", "lapses": 0}
        rec = self.answer(rec, False, "2026-04-01")
        self.assertEqual((rec["phase"], rec["pending"], rec["due"], rec["lapses"]), ("relearning", 30, "2026-04-02", 1))
        passed = self.answer(rec, True, "2026-04-02")
        self.assertEqual((passed["phase"], passed["interval"], passed["due"]), ("review", 30, "2026-05-02"))

    def test_repeated_lapses_divide_again_down_to_one_day(self):
        rec = {"phase": "review", "interval": 90, "last": "2026-01-01", "due": "2026-04-01", "lapses": 0}
        rec = self.answer(rec, False, "2026-04-01")
        pendings = [rec["pending"]]
        day = D("2026-04-02")
        for _ in range(4):
            rec = srs.apply_answer(rec, False, day, self.s, "X")
            pendings.append(rec["pending"])
            day = D(rec["due"])
        self.assertEqual(pendings, [30, 10, 3, 1, 1])
        self.assertEqual(rec["lapses"], 5)

    def test_overdue_pass_averages_scheduled_and_elapsed(self):
        rec = {"phase": "review", "interval": 10, "last": "2026-01-01", "due": "2026-01-11"}
        rec = self.answer(rec, True, "2026-01-21")  # 20 days elapsed: base 15, x2.5 = 37.5
        self.assertEqual(rec["interval"], 38)

    def test_overdue_fail_cuts_the_scheduled_interval(self):
        rec = {"phase": "review", "interval": 30, "last": "2026-01-01", "due": "2026-01-31"}
        rec = self.answer(rec, False, "2026-03-31")
        self.assertEqual(rec["pending"], 10)

    def test_fuzz_is_deterministic_and_bounded(self):
        p = srs.THIRDS_DEFAULTS
        for interval in (3, 10, 40, 200):
            values = {srs.fuzzed(interval, f"c{i}", D("2026-01-01"), p) for i in range(200)}
            self.assertTrue(all(abs(v - interval) <= round(interval * 0.05) + 1 for v in values))
        self.assertEqual(srs.fuzzed(2, "c", D("2026-01-01"), p), 2)
        self.assertEqual(srs.fuzzed(40, "c", D("2026-01-01"), p), srs.fuzzed(40, "c", D("2026-01-01"), p))

    def test_review_pass_always_grows(self):
        s = sched("thirds")
        for interval in range(1, 60):
            rec = {"phase": "review", "interval": interval, "last": "2026-01-01"}
            day = D("2026-01-01") + dt.timedelta(days=interval)
            self.assertGreater(srs.apply_answer(rec, True, day, s, f"n{interval}")["interval"], interval)


class AnkiTest(unittest.TestCase):
    def setUp(self):
        self.s = sched("anki", fuzz=False)

    def answer(self, rec, passed, day, s=None):
        return srs.apply_answer(rec, passed, D(day), s or self.s, "X")

    def review(self, interval, due, ease=2.5, lapses=0):
        last = (D(due) - dt.timedelta(days=interval)).isoformat()
        return {"phase": "review", "interval": interval, "ease": ease, "last": last, "due": due, "lapses": lapses}

    def test_new_pass_graduates_with_the_starting_ease(self):
        rec = self.answer(srs.new_record("integrate", D("2026-01-01"), 1), True, "2026-01-02")
        self.assertEqual((rec["phase"], rec["interval"], rec["due"], rec["ease"]), ("review", 1, "2026-01-03", 2.5))

    def test_new_fail_goes_to_learning_tomorrow(self):
        rec = self.answer(srs.new_record("integrate", D("2026-01-01"), 1), False, "2026-01-02")
        self.assertEqual((rec["phase"], rec["due"]), ("learning", "2026-01-03"))

    def test_always_right_sequence_matches_thirds(self):
        rec = srs.new_record("integrate", D("2026-01-01"), 1)
        day = D("2026-01-02")
        seen = []
        for _ in range(7):
            rec = srs.apply_answer(rec, True, day, self.s, "X")
            seen.append(rec["interval"])
            day = D(rec["due"])
        self.assertEqual(seen, [1, 3, 8, 20, 50, 125, 313])
        self.assertEqual(rec["ease"], 2.5)

    def test_lapse_restarts_at_one_day_and_lowers_the_ease(self):
        rec = self.answer(self.review(90, "2026-04-01"), False, "2026-04-01")
        self.assertEqual((rec["phase"], rec["pending"], rec["ease"], rec["lapses"], rec["due"]),
                         ("relearning", 1, 2.3, 1, "2026-04-02"))
        seen = []
        day = "2026-04-02"
        for _ in range(3):
            rec = self.answer(rec, True, day)
            seen.append(rec["interval"])
            day = rec["due"]
        self.assertEqual(seen, [1, 3, 7])  # 1 x 2.3 is raised to the minimum growth; then 3 x 2.3 = 6.9

    def test_relearning_fail_repeats_the_step_without_a_new_lapse(self):
        rec = {"phase": "relearning", "interval": 90, "pending": 1, "ease": 2.3, "lapses": 1, "due": "2026-04-02"}
        rec = self.answer(rec, False, "2026-04-02")
        self.assertEqual((rec["phase"], rec["pending"], rec["ease"], rec["lapses"], rec["due"]),
                         ("relearning", 1, 2.3, 1, "2026-04-03"))

    def test_ease_never_drops_below_the_minimum(self):
        rec = self.review(10, "2026-01-01")
        day = D("2026-01-01")
        eases = []
        for _ in range(7):
            rec = srs.apply_answer(rec, False, day, self.s, "X")
            eases.append(rec["ease"])
            rec = srs.apply_answer(rec, True, day + dt.timedelta(days=1), self.s, "X")
            day = D(rec["due"])
        self.assertEqual(eases, [2.3, 2.1, 1.9, 1.7, 1.5, 1.3, 1.3])
        self.assertEqual(rec["lapses"], 7)

    def test_overdue_pass_counts_half_the_delay(self):
        rec = self.answer(self.review(10, "2026-01-11"), True, "2026-01-21")  # (10 + 10/2) x 2.5 = 37.5
        self.assertEqual(rec["interval"], 38)

    def test_hard_interval_sets_the_minimum_growth(self):
        rec = self.review(3, "2026-01-10", ease=1.3)
        self.assertEqual(self.answer(rec, True, "2026-01-10")["interval"], 5)  # 3.9, raised to Hard (4) + 1
        no_hard = sched("anki", fuzz=False, hard_interval=1.0)
        self.assertEqual(self.answer(rec, True, "2026-01-10", no_hard)["interval"], 4)

    def test_parameters_change_the_rules(self):
        keep_half = sched("anki", fuzz=False, new_interval=0.5)
        rec = self.answer(self.review(40, "2026-01-10"), False, "2026-01-10", keep_half)
        self.assertEqual(rec["pending"], 20)
        self.assertEqual(self.answer(rec, True, "2026-01-11", keep_half)["interval"], 20)
        slower = sched("anki", fuzz=False, interval_modifier=0.8)
        self.assertEqual(self.answer(self.review(20, "2026-01-10"), True, "2026-01-10", slower)["interval"], 40)
        capped = sched("anki", fuzz=False, maximum_interval=100)
        self.assertEqual(self.answer(self.review(80, "2026-01-10"), True, "2026-01-10", capped)["interval"], 100)
        no_penalty = sched("anki", fuzz=False, ease_penalty=0)
        self.assertEqual(self.answer(self.review(40, "2026-01-10"), False, "2026-01-10", no_penalty)["ease"], 2.5)

    def test_fuzz_follows_ankis_ranges(self):
        s = sched("anki")
        day = D("2026-01-10")
        values = {srs.apply_answer(self.review(20, "2026-01-10"), True, day, s, f"c{i}")["interval"]
                  for i in range(300)}
        self.assertTrue(values <= set(range(46, 55)), values)  # 50, plus or minus 4.475 days
        self.assertGreater(len(values), 4)
        short = {srs.apply_answer(self.review(1, "2026-01-10"), True, day, s, f"c{i}")["interval"]
                 for i in range(300)}
        self.assertEqual(short, {3, 4})
        new = {srs.apply_answer(srs.new_record("integrate", D("2026-01-01"), 1), True, day, s, f"c{i}")["interval"]
               for i in range(50)}
        self.assertEqual(new, {1})  # under 2.5 days: no fuzz
        one = srs.apply_answer(self.review(20, "2026-01-10"), True, day, s, "same")
        self.assertEqual(one, srs.apply_answer(self.review(20, "2026-01-10"), True, day, s, "same"))

    def test_review_pass_always_grows(self):
        for ease in (1.3, 2.5):
            for interval in range(1, 60):
                due = (D("2026-01-01") + dt.timedelta(days=interval)).isoformat()
                rec = self.review(interval, due, ease=ease)
                grown = srs.apply_answer(rec, True, D(due), sched("anki"), f"n{interval}")["interval"]
                self.assertGreater(grown, interval)


class UpdateTest(unittest.TestCase):
    def test_update_holds_a_review_concept_until_tomorrow(self):
        rec = {"phase": "review", "interval": 40, "due": "2026-03-01", "last": "2026-01-20"}
        rec = srs.apply_update(rec, D("2026-02-01"), "new section on X")
        self.assertEqual((rec["phase"], rec["interval"], rec["due"], rec["focus"], rec["hold"]),
                         ("review", 40, "2026-02-02", "new section on X", True))

    def test_update_only_sets_focus_outside_review(self):
        rec = {"phase": "learning", "due": "2026-02-02"}
        rec = srs.apply_update(rec, D("2026-02-01"), "more")
        self.assertEqual((rec["phase"], rec["due"], rec["focus"]), ("learning", "2026-02-02", "more"))
        self.assertNotIn("hold", rec)

    def test_a_pass_on_hold_keeps_the_interval(self):
        for algorithm in ("anki", "thirds"):
            rec = {"phase": "review", "interval": 40, "ease": 2.1, "due": "2026-03-01", "last": "2026-01-20"}
            rec = srs.apply_update(rec, D("2026-02-01"), "new part")
            rec = srs.apply_answer(rec, True, D("2026-02-02"), sched(algorithm), "X")
            self.assertEqual((rec["phase"], rec["interval"], rec["due"], rec["focus"]),
                             ("review", 40, "2026-03-14", None), algorithm)
            self.assertNotIn("hold", rec)
            self.assertEqual(rec["ease"], 2.1)

    def test_a_fail_on_hold_is_a_normal_lapse(self):
        base = {"phase": "review", "interval": 40, "ease": 2.5, "due": "2026-03-01", "last": "2026-01-20", "lapses": 0}
        held = srs.apply_update(base, D("2026-02-01"), "new part")
        thirds = srs.apply_answer(held, False, D("2026-02-02"), sched("thirds"), "X")
        self.assertEqual((thirds["phase"], thirds["pending"], thirds["lapses"]), ("relearning", 13, 1))
        anki = srs.apply_answer(held, False, D("2026-02-02"), sched("anki"), "X")
        self.assertEqual((anki["phase"], anki["pending"], anki["lapses"], anki["ease"]), ("relearning", 1, 1, 2.3))
        self.assertNotIn("hold", anki)

    def test_unknown_algorithm_is_refused(self):
        rec = srs.new_record("integrate", D("2026-01-01"), 1)
        with self.assertRaises(srs.SrsError) as ctx:
            srs.apply_answer(rec, True, D("2026-01-02"), dict(sched(), algorithm="sm17"), "X")
        self.assertIn("anki, thirds", str(ctx.exception))
        self.assertEqual(srs.algorithm(dict(sched(), algorithm=" Anki "))[0], "anki")


class DailyListTest(unittest.TestCase):
    def make(self, concepts):
        return {"version": 1, "next_order": 100, "concepts": concepts, "flags": {"thin": {}}}

    def test_order_learning_then_reviews_then_new(self):
        state = self.make({
            "rev-old": {"phase": "review", "due": "2026-01-01", "interval": 5},
            "rev-new": {"phase": "review", "due": "2026-01-09", "interval": 2},
            "relearn": {"phase": "relearning", "due": "2026-01-10", "pending": 3},
            "new-promote": dict(srs.new_record("promote", D("2026-01-01"), 1)),
            "new-integrate": dict(srs.new_record("integrate", D("2026-01-05"), 2)),
            "future": {"phase": "review", "due": "2026-02-01", "interval": 30},
        })
        items, info = srs.daily_list(state, sched(), D("2026-01-10"))
        self.assertEqual(items, ["relearn", "rev-old", "rev-new", "new-integrate", "new-promote"])
        self.assertEqual(info["carried"], 0)

    def test_updated_concepts_come_with_relearning(self):
        state = self.make({
            "rev-old": {"phase": "review", "due": "2026-01-01", "interval": 5},
            "updated": {"phase": "review", "due": "2026-01-10", "interval": 30, "hold": True},
            "relearn": {"phase": "relearning", "due": "2026-01-10", "pending": 3},
        })
        items, info = srs.daily_list(state, sched(), D("2026-01-10"))
        self.assertEqual(items, ["relearn", "updated", "rev-old"])
        self.assertEqual((info["learning"], info["reviews"]), (2, 1))

    def test_new_concepts_wait_until_the_next_day(self):
        state = self.make({"n": srs.new_record("integrate", D("2026-01-10"), 1)})
        self.assertEqual(srs.daily_list(state, sched(), D("2026-01-10"))[0], [])
        self.assertEqual(srs.daily_list(state, sched(), D("2026-01-11"))[0], ["n"])

    def test_daily_limit_carries_reviews_and_blocks_new(self):
        concepts = {f"r{i:02d}": {"phase": "review", "due": "2026-01-01", "interval": 3} for i in range(25)}
        concepts["n"] = srs.new_record("integrate", D("2026-01-01"), 1)
        items, info = srs.daily_list(self.make(concepts), sched(), D("2026-01-10"))
        self.assertEqual(len(items), 20)
        self.assertEqual(info["carried"], 5)
        self.assertNotIn("n", items)

    def test_new_per_day_cap_counts_concepts_started_today(self):
        concepts = {f"n{i}": srs.new_record("integrate", D("2026-01-01"), i) for i in range(10)}
        for i in range(3):
            concepts[f"n{i}"].update(phase="review", introduced="2026-01-10", last="2026-01-10", due="2026-01-11")
        items, info = srs.daily_list(self.make(concepts), sched(), D("2026-01-10"))
        self.assertEqual(len(items), 2)  # 5 per day, 3 already started today
        self.assertEqual(info["done"], 3)


class IndexTest(unittest.TestCase):
    TEXT = "\n".join([
        "# Index",
        "intro text is fine",
        "```text",
        "## [[Page name]]",
        "- Concept",
        "```",
        "## [[Eigendecomposition]]",
        "- Eigenvectors",
        "- Reversibility (calculus) — integral and derivative as inverses",
        "not a concept line",
        "## [[Math/Bayes|alias]]",
        "* Bayes' theorem",
        "# Other heading",
        "- stray concept",
    ])

    def test_parse(self):
        idx = srs.parse_index(self.TEXT)
        self.assertEqual([p.link for p in idx.pages], ["Eigendecomposition", "Math/Bayes"])
        concepts = [(c.name, c.desc) for c in idx.concepts()]
        self.assertEqual(concepts, [("Eigenvectors", None),
                                    ("Reversibility (calculus)", "integral and derivative as inverses"),
                                    ("Bayes' theorem", None)])
        self.assertEqual(len(idx.problems), 2)
        self.assertIn("unrecognised", idx.problems[0][1])
        self.assertIn("outside a page section", idx.problems[1][1])

    def test_patterns(self):
        m = srs.matches_pattern
        self.assertTrue(m("Templates/daily.md", "Templates/"))
        self.assertTrue(m("templates/x/y.md", "Templates/"))
        self.assertFalse(m("MyTemplates/a.md", "Templates/"))
        self.assertTrue(m("Draw/a.excalidraw.md", "*.excalidraw.md"))
        self.assertTrue(m("Daily/2026/01.md", "Daily/*.md"))
        self.assertTrue(m("x/Archive/a.md", "Archive"))
        self.assertFalse(m("notes/a.md", ""))


class VaultCase(unittest.TestCase):
    """A temporary git vault with Recall set up."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        (self.vault / "Math").mkdir(parents=True)
        self.env = dict(os.environ, **GIT_ENV)
        self.write("Math/Eigendecomposition.md", "# Eigen\nAv = λv defines eigenvectors.\n")
        self.write("Math/Bayes.md", "# Bayes\nP(A|B) = P(B|A)P(A)/P(B)\n")
        self.write("Templates/t.md", "{{date}}\n")
        self.write("inbox.md", "Intro.\n\n## First\none\n\n## Second\ntwo\n")
        self.srs("init", "--git-init", "--exclude", "Templates/")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, rel, text):
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def srs(self, *args, today="2026-01-10", check=True, cwd=None):
        proc = subprocess.run([sys.executable, str(SRS), "--today", today, *args], cwd=str(cwd or self.vault),
                              env=self.env, capture_output=True, text=True)
        if check and proc.returncode != 0:
            self.fail(f"srs {' '.join(args)} failed ({proc.returncode}): {proc.stderr}")
        return proc

    def guard(self, payload, plugin_root=ROOT):
        env = dict(self.env, CLAUDE_PLUGIN_ROOT=str(plugin_root))
        proc = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(payload), env=env,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] if proc.stdout.strip() else None

    def edit_via_hook(self, rel, text, mode="w"):
        decision = self.guard({"tool_name": "Write", "tool_input": {"file_path": str(self.vault / rel)},
                               "cwd": str(self.vault)})
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open(mode, encoding="utf-8") as fh:
            fh.write(text)
        return decision

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=str(self.vault), env=self.env, capture_output=True,
                              text=True).stdout

    def index(self, body):
        self.write("index.md", "# Index\n\n" + body)


class GitFlowTest(VaultCase):
    def test_init_commits_snapshot_then_setup(self):
        log = self.git("log", "--format=%s").splitlines()
        self.assertEqual(log, ["setup: initialize Recall", "vault: snapshot before Recall setup"])
        self.assertIn(".srs/session.json", (self.vault / ".gitignore").read_text())
        self.assertEqual(self.srs("init", check=False).returncode, 2)

    def test_begin_commits_manual_edits(self):
        self.write("Math/Bayes.md", "edited by hand\n")
        out = self.srs("begin", "quiz").stdout
        self.assertIn("manual edits", out)
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), "vault: manual edits")
        self.srs("end")

    def test_commit_takes_only_touched_files(self):
        self.srs("begin", "integrate")
        self.assertEqual(self.edit_via_hook("wiki/New.md", "# New\n"), "allow")
        self.write("Math/Bayes.md", "concurrent edit in Obsidian\n")  # not touched by Claude
        self.assertIn("wiki/New.md", self.srs("diff").stdout)
        self.srs("commit", "-m", "integrate: New")
        files = self.git("show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(files, ["wiki/New.md"])
        self.srs("end")
        self.assertIn("Math/Bayes.md", self.git("status", "--porcelain"))

    def test_discard_restores_and_removes_only_touched_files(self):
        self.srs("begin", "integrate")
        self.edit_via_hook("Math/Eigendecomposition.md", "changed\n")
        self.edit_via_hook("wiki/Fresh.md", "# Fresh\n")
        self.write("Math/Bayes.md", "user edit\n")
        self.srs("discard")
        self.assertIn("Av =", (self.vault / "Math/Eigendecomposition.md").read_text())
        self.assertFalse((self.vault / "wiki/Fresh.md").exists())
        self.assertEqual((self.vault / "Math/Bayes.md").read_text(), "user edit\n")
        self.srs("end")

    def test_discard_removes_folders_left_empty(self):
        self.srs("begin", "integrate")
        self.edit_via_hook("Physics/Optics/Lenses.md", "# Lenses\n")
        self.edit_via_hook("Math/Fresh.md", "# Fresh\n")
        out = self.srs("discard").stdout
        self.assertIn("removed empty folders Physics/Optics, Physics", out)
        self.assertFalse((self.vault / "Physics").exists())
        self.assertFalse((self.vault / "Math/Fresh.md").exists())
        self.assertTrue((self.vault / "Math").is_dir())  # still holds other pages
        self.srs("end")

    def test_discard_keeps_folders_that_still_hold_files(self):
        self.write("Physics/Old.md", "# Old\n")
        self.srs("begin", "integrate")  # commits Physics/Old.md as a manual edit
        self.edit_via_hook("Physics/Optics/Lenses.md", "# Lenses\n")
        self.srs("discard")
        self.assertFalse((self.vault / "Physics/Optics").exists())
        self.assertTrue((self.vault / "Physics/Old.md").exists())
        self.srs("end")

    def test_abort_removes_folders_left_empty(self):
        self.srs("begin", "integrate")
        self.edit_via_hook("Physics/Lenses.md", "# Lenses\n")
        self.assertIn("1 empty folder", self.srs("abort").stdout)
        self.assertFalse((self.vault / "Physics").exists())

    def test_end_refuses_with_uncommitted_session_changes(self):
        self.srs("begin", "integrate")
        self.edit_via_hook("wiki/X.md", "x\n")
        self.assertEqual(self.srs("end", check=False).returncode, 2)
        self.srs("discard")
        self.srs("end")

    def test_interrupted_session_blocks_begin_and_abort_cleans(self):
        self.srs("begin", "integrate")
        self.edit_via_hook("wiki/X.md", "x\n")
        proc = self.srs("begin", "quiz", check=False)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("INTERRUPTED SESSION", proc.stderr)
        self.assertIn("INTERRUPTED SESSION", self.srs("context", "quiz").stdout)
        self.srs("abort")
        self.assertFalse((self.vault / "wiki/X.md").exists())
        self.srs("begin", "quiz")
        self.srs("end")

    def test_vault_in_a_subfolder_of_a_bigger_repository(self):
        outer = self.tmp / "outer"
        inner = outer / "notes" / "vault"
        inner.mkdir(parents=True)
        (outer / "README").write_text("outer\n")
        subprocess.run(["git", "init", "-q"], cwd=str(outer), env=self.env, check=True)
        (inner / "a.md").write_text("# A\n")
        self.srs("init", cwd=inner)
        self.srs("begin", "integrate", cwd=inner)
        decision = self.guard({"tool_name": "Write", "tool_input": {"file_path": str(inner / "b.md")}, "cwd": str(inner)})
        self.assertEqual(decision, "allow")
        (inner / "b.md").write_text("# B\n")
        self.srs("commit", "-m", "integrate: B", cwd=inner)
        self.srs("end", cwd=inner)
        files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=str(outer), env=self.env,
                               capture_output=True, text=True).stdout.split()
        self.assertEqual(files, ["notes/vault/b.md"])


class ConfigTest(VaultCase):
    def config(self, vault=None):
        return json.loads(((vault or self.vault) / ".srs/config.json").read_text())

    def test_default_algorithm_is_anki_with_both_parameter_sets(self):
        scheduling = self.config()["scheduling"]
        self.assertEqual(scheduling["algorithm"], "anki")
        self.assertEqual(scheduling["anki"], srs.ANKI_DEFAULTS)
        self.assertEqual(scheduling["thirds"], srs.THIRDS_DEFAULTS)
        self.assertIn("Scheduling algorithm: anki", self.srs("stats").stdout)

    def test_init_takes_the_algorithm(self):
        other = self.tmp / "other"
        other.mkdir()
        (other / "a.md").write_text("# A\n")
        out = self.srs("init", "--git-init", "--algorithm", "thirds", cwd=other).stdout
        self.assertIn("Scheduling algorithm: thirds", out)
        self.assertEqual(self.config(other)["scheduling"]["algorithm"], "thirds")
        self.assertEqual(self.srs("init", "--algorithm", "sm2", cwd=self.tmp, check=False).returncode, 2)

    def test_new_page_placement_defaults_to_folders_and_init_takes_it(self):
        self.assertEqual(self.config()["new_page_placement"], "folders")
        other = self.tmp / "other"
        other.mkdir()
        (other / "a.md").write_text("# A\n")
        out = self.srs("init", "--git-init", "--new-page-placement", "flat", cwd=other).stdout
        self.assertIn("New page placement: flat", out)
        self.assertEqual(self.config(other)["new_page_placement"], "flat")
        self.assertEqual(self.config(other)["paths"]["new_pages"], "wiki")
        self.assertEqual(self.srs("init", "--new-page-placement", "tree", cwd=self.tmp, check=False).returncode, 2)

    def test_unknown_placement_is_reported(self):
        config = self.config()
        config["new_page_placement"] = "tree"
        (self.vault / ".srs/config.json").write_text(json.dumps(config))
        self.assertIn("ERROR: Unknown new_page_placement 'tree'", self.srs("context", "integrate").stdout)

    def test_leech_threshold_follows_the_algorithm(self):
        self.index("## [[Bayes]]\n- Bayes' theorem\n")
        self.srs("add", "--source", "promote", "Bayes' theorem")
        state = json.loads((self.vault / ".srs/state.json").read_text())
        state["concepts"]["Bayes' theorem"].update(phase="review", interval=3, due="2026-01-10", lapses=8)
        (self.vault / ".srs/state.json").write_text(json.dumps(state))
        self.assertIn("Leeches (8+ lapses)", self.srs("check").stdout)
        config = self.config()
        config["scheduling"]["algorithm"] = "thirds"
        (self.vault / ".srs/config.json").write_text(json.dumps(config))
        self.assertNotIn("Bayes' theorem (8 lapses)", self.srs("check").stdout)

    def test_unknown_algorithm_is_reported(self):
        config = self.config()
        config["scheduling"]["algorithm"] = "sm17"
        (self.vault / ".srs/config.json").write_text(json.dumps(config))
        proc = self.srs("stats", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Unknown scheduling algorithm 'sm17'", proc.stderr)
        self.assertIn("ERROR: Unknown scheduling algorithm", self.srs("context", "maintenance").stdout)


class InboxTest(VaultCase):
    def ids(self):
        out = self.srs("inbox", "list").stdout.splitlines()[1:]
        return [line.split("[")[1].split("]")[0] for line in out]

    def test_list_is_newest_first_and_fences_are_ignored(self):
        self.write("inbox.md", "Intro\n\n## A\ntext\n```\n## not a heading\n```\n\n### sub\n\n## B\nmore\n")
        out = self.srs("inbox", "list").stdout
        self.assertLess(out.index("] B"), out.index("] A"))
        self.assertEqual(len(self.ids()), 2)

    def test_show_prints_one_block_as_is_and_labels_several_by_id(self):
        self.write("inbox.md", "## Same title\nfirst\n\n## Same title\nsecond\n\n## Other\nthird\n")
        newest, middle, oldest = self.ids()
        self.assertEqual(self.srs("inbox", "show", middle).stdout, "## Same title\nsecond\n")
        out = self.srs("inbox", "show", oldest, middle, newest).stdout
        self.assertEqual(
            out,
            f"[{oldest}]\n## Same title\nfirst\n\n[{middle}]\n## Same title\nsecond\n\n[{newest}]\n## Other\nthird\n",
        )
        self.assertEqual(self.srs("inbox", "show", oldest, "nope", check=False).returncode, 2)

    def test_remove_keeps_everything_else_byte_for_byte(self):
        self.write("inbox.md", "Intro\r\n\r\n## A\r\none\r\n\r\n## B\r\ntwo\r\n")
        newest, oldest = self.ids()
        self.srs("begin", "integrate")
        self.srs("inbox", "remove", oldest)
        self.assertEqual((self.vault / "inbox.md").read_bytes(), b"Intro\r\n\r\n## B\r\ntwo\r\n")
        self.assertEqual(self.srs("inbox", "remove", oldest, check=False).returncode, 2)
        self.srs("commit", "-m", "integrate: A")
        self.srs("end")

    def test_removing_the_last_block_leaves_no_trailing_blank_lines(self):
        self.write("inbox.md", "Intro\n\n## A\none\n\n## B\ntwo\n\n\n")
        newest, _ = self.ids()
        self.srs("inbox", "remove", newest)
        self.assertEqual((self.vault / "inbox.md").read_text(), "Intro\n\n## A\none\n")

    def test_heading_level_comes_from_config(self):
        config = json.loads((self.vault / ".srs/config.json").read_text())
        config["inbox_heading_level"] = 3
        (self.vault / ".srs/config.json").write_text(json.dumps(config))
        self.write("inbox.md", "## Week 1\n### Topic A\na\n### Topic B\nb\n## Week 2\n### Topic C\nc\n")
        self.assertEqual(len(self.ids()), 3)


class ReviewFlowTest(VaultCase):
    def setUp(self):
        super().setUp()
        self.index("## [[Eigendecomposition]]\n- Eigenvectors\n\n## [[Bayes]]\n- Bayes' theorem\n")
        self.srs("begin", "setup")
        self.srs("end")  # commits the index as a manual edit

    def test_add_record_and_refusals(self):
        self.srs("begin", "promote")
        self.assertEqual(self.srs("add", "--source", "promote", "Nope", check=False).returncode, 2)
        self.srs("add", "--source", "promote", "eigenvectors", "Bayes' theorem")
        self.srs("commit", "-m", "promote: 2 concepts")
        self.srs("end")
        self.assertIn("Nothing to ask today", self.srs("due").stdout)
        tomorrow = "2026-01-11"
        self.srs("begin", "quiz", today=tomorrow)
        self.srs("record", "Eigenvectors", "pass", "--question", "what defines an eigenvector", today=tomorrow)
        self.assertEqual(self.srs("record", "Eigenvectors", "fail", today=tomorrow, check=False).returncode, 2)
        self.srs("record", "Bayes' theorem", "fail", today=tomorrow)
        self.assertIn("Score: 1/2", self.srs("today", today=tomorrow).stdout)
        self.srs("commit", "-m", "quiz: 1/2", today=tomorrow)
        self.srs("end", today=tomorrow)
        state = json.loads((self.vault / ".srs/state.json").read_text())
        self.assertEqual(state["concepts"]["Eigenvectors"]["questions"], ["what defines an eigenvector"])
        log = (self.vault / ".srs/log.csv").read_text().splitlines()
        self.assertEqual(log[1:], ["2026-01-11,Eigenvectors,pass,new", "2026-01-11,Bayes' theorem,fail,new"])
        files = self.git("show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(sorted(files), [".srs/log.csv", ".srs/state.json"])

    def test_record_refuses_concepts_not_due(self):
        self.srs("add", "--source", "promote", "Eigenvectors")
        self.assertEqual(self.srs("record", "Eigenvectors", "pass", check=False).returncode, 2)

    def test_rename_and_remove(self):
        self.srs("add", "--source", "integrate", "Eigenvectors")
        self.assertEqual(self.srs("rename", "Eigenvectors", "Eigenvector", check=False).returncode, 2)
        self.index("## [[Eigendecomposition]]\n- Eigenvector\n\n## [[Bayes]]\n- Bayes' theorem\n")
        self.srs("rename", "Eigenvectors", "Eigenvector")
        state = json.loads((self.vault / ".srs/state.json").read_text())
        self.assertIn("Eigenvector", state["concepts"])
        self.srs("remove", "eigenvector")
        state = json.loads((self.vault / ".srs/state.json").read_text())
        self.assertEqual(state["concepts"], {})


class ChecksTest(VaultCase):
    def test_checks_seals_and_exclusions(self):
        self.index("\n".join([
            "## [[Eigendecomposition]]",
            "- Eigenvectors — " + "x" * 70,
            "## [[Bayes]]",
            "- Eigenvectors",
            "## [[Missing page]]",
            "- Ghost",
            "## [[t]]",
            "- Template concept",
        ]) + "\n")
        self.write("Math/Big.md", "y" * 7000)
        self.write("inbox.md", "z" * 9000)  # the inbox is never checked
        report = srs_report(self.vault)
        self.assertEqual([p for p, _ in report["over_size"]], [])  # Big.md is not indexed
        self.assertIn("Math/Big.md", report["unindexed"])
        self.assertNotIn("inbox.md", report["unindexed"])
        self.assertNotIn("Templates/t.md", report["unindexed"])
        self.assertEqual(report["duplicates"][0][0], "Eigenvectors")
        statuses = {p.link: p.status for p in report["link_problems"]}
        self.assertEqual(statuses, {"Missing page": "missing", "t": "excluded"})
        self.assertEqual(len(report["long_descriptions"]), 1)
        out = self.srs("verify", "Math/Bayes.md", check=False)
        self.assertEqual(out.returncode, 2)
        self.assertIn("also used elsewhere", out.stdout)

    def test_seal_breaks_when_page_or_entry_changes(self):
        self.index("## [[Eigendecomposition]]\n- Eigenvectors\n\n## [[Bayes]]\n- Bayes' theorem\n")
        self.srs("verify", "Math/Bayes.md", "Eigendecomposition")
        self.assertEqual([p.path for p in srs_report(self.vault)["unverified"]], [])
        self.write("Math/Bayes.md", "# Bayes\nchanged\n")
        self.assertEqual([p.path for p in srs_report(self.vault)["unverified"]], ["Math/Bayes.md"])
        self.index("## [[Eigendecomposition]]\n- Eigenvectors\n- Eigenvalues\n\n## [[Bayes]]\n- Bayes' theorem\n")
        self.assertIn("Math/Eigendecomposition.md", [p.path for p in srs_report(self.vault)["unverified"]])

    def test_a_new_duplicate_unseals_the_sealed_page(self):
        self.index("## [[Eigendecomposition]]\n- Eigenvectors\n\n## [[Bayes]]\n- Bayes' theorem\n")
        self.srs("verify", "Math/Bayes.md")
        self.index("## [[Eigendecomposition]]\n- Eigenvectors\n- Bayes' theorem\n\n## [[Bayes]]\n- Bayes' theorem\n")
        self.assertIn("Math/Bayes.md", [p.path for p in srs_report(self.vault)["unverified"]])
        self.srs("unseal", "Bayes")
        verified = json.loads((self.vault / ".srs/verified.json").read_text())
        self.assertEqual(verified, {})

    def test_ambiguous_links_need_a_path(self):
        self.write("Other/Bayes.md", "# Another Bayes\n")
        self.index("## [[Bayes]]\n- Bayes' theorem\n\n## [[Other/Bayes]]\n- Bayes factor\n")
        report = srs_report(self.vault)
        self.assertEqual([(p.link, p.status) for p in report["link_problems"]], [("Bayes", "ambiguous")])

    def test_maintenance_context_batches_only_clean_unverified_pages(self):
        self.index("## [[Eigendecomposition]]\n- Eigenvectors\n\n## [[Bayes]]\n- Eigenvectors\n")
        self.write("Math/Big.md", "y" * 7000)
        out = self.srs("context", "maintenance").stdout
        batch = out.split("## Batch to audit")[1]
        self.assertNotIn("Math/Bayes.md", batch)


def srs_report(vault):
    config = srs.load_config(vault)
    v = srs.Vault(root=vault, config=config, today=D("2026-01-10"))
    return srs.run_checks(v)


class GuardTest(VaultCase):
    def test_denies_edits_to_srs(self):
        decision = self.guard({"tool_name": "Edit", "tool_input": {"file_path": str(self.vault / ".srs/state.json")},
                               "cwd": str(self.vault)})
        self.assertEqual(decision, "deny")

    def test_no_opinion_without_a_session_or_outside_vaults(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": str(self.vault / "Math/Bayes.md")},
                   "cwd": str(self.vault)}
        self.assertIsNone(self.guard(payload))
        outside = self.tmp / "elsewhere.md"
        self.assertIsNone(self.guard({"tool_name": "Write", "tool_input": {"file_path": str(outside)},
                                      "cwd": str(self.tmp)}))

    def test_session_allows_notes_but_not_hidden_or_non_markdown(self):
        self.srs("begin", "integrate")
        base = {"tool_name": "Edit", "cwd": str(self.vault)}
        self.assertEqual(self.guard(dict(base, tool_input={"file_path": str(self.vault / "Math/Bayes.md")})), "allow")
        self.assertIsNone(self.guard(dict(base, tool_input={"file_path": str(self.vault / ".obsidian/app.json")})))
        self.assertIsNone(self.guard(dict(base, tool_input={"file_path": str(self.vault / "data.csv")})))
        session = json.loads((self.vault / ".srs/session.json").read_text())
        self.assertEqual(session["touched"], ["Math/Bayes.md"])
        self.srs("end")

    def test_allows_only_the_plugin_script_on_its_own(self):
        script = ROOT / "scripts" / "srs.py"
        bash = lambda cmd: self.guard({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(self.vault)})
        self.assertEqual(bash(f'python3 {script} due'), "allow")
        self.assertEqual(bash(f'python3 "{script}" record "Bayes\' theorem" pass --question "what is it"'), "allow")
        self.assertIsNone(bash(f'python3 {script} due && rm -rf ~'))
        self.assertIsNone(bash(f'python3 {script} due; ls'))
        self.assertIsNone(bash(f'python3 {script} due > out.txt'))
        self.assertIsNone(bash(f'python3 {script} $(whoami)'))
        self.assertIsNone(bash('python3 /tmp/other/srs.py due'))
        self.assertIsNone(bash('rm -rf /'))


class ContextTest(VaultCase):
    def test_contexts_print_without_errors(self):
        self.index("## [[Bayes]]\n- Bayes' theorem\n")
        for command in ("setup", "integrate", "quiz", "promote", "maintenance"):
            out = self.srs("context", command).stdout
            self.assertIn(f"# Recall context: /{command}", out)
            self.assertIn("## Rules", out)
            self.assertNotIn("ERROR", out)

    def test_integrate_context_marks_scheduled_concepts(self):
        self.index("## [[Eigendecomposition]]\n- Eigenvectors\n\n## [[Bayes]]\n- Bayes' theorem\n")
        self.srs("add", "--source", "integrate", "Eigenvectors")
        out = self.srs("context", "integrate", "3").stdout
        self.assertIn("- Eigenvectors  [queued]", out)
        self.assertIn("Process up to: 3", out)
        self.assertIn("] Second", out)

    def test_integrate_context_lists_folders_two_levels_deep(self):
        self.write("Math/Algebra/Groups/Cyclic.md", "# Cyclic\n")
        self.write("Math/Algebra/Rings.md", "# Rings\n")
        self.write("Math/Algebra/Fields.md", "# Fields\n")
        self.write(".obsidian/plugins/x.md", "hidden\n")
        self.write("Attachments/note.md", "excluded\n")
        self.write("Loose.md", "# Loose\n")
        config = json.loads((self.vault / ".srs/config.json").read_text())
        config["exclude"].append("Attachments/")
        (self.vault / ".srs/config.json").write_text(json.dumps(config))
        out = self.srs("context", "integrate").stdout
        self.assertIn("placement: folders", out)
        self.assertIn("\n  (vault root): 1 page\n", out)
        self.assertIn("\n  Math/ — 5 pages\n    Algebra/ — 3 pages\n", out)
        self.assertNotIn("Groups", out)  # third level
        self.assertNotIn("Templates", out)
        self.assertNotIn("Attachments", out)
        self.assertNotIn(".obsidian", out)

    def test_flat_placement_points_to_the_new_pages_folder_only(self):
        config = json.loads((self.vault / ".srs/config.json").read_text())
        config["new_page_placement"] = "flat"
        (self.vault / ".srs/config.json").write_text(json.dumps(config))
        out = self.srs("context", "integrate").stdout
        self.assertIn("New pages go in: wiki/ (placement: flat", out)
        self.assertNotIn("## Folders", out)

    def test_setup_context_offers_placement_and_suggests_folders(self):
        bare = self.tmp / "bare"
        (bare / "Notes").mkdir(parents=True)
        (bare / "Notes/a.md").write_text("# A\n")
        out = self.srs("context", "setup", cwd=bare).stdout
        self.assertIn("--new-page-placement; default folders", out)
        self.assertIn("  flat: ", out)
        self.assertIn("Suggested: folders (the vault has pages in folders such as Notes)", out)

    def test_not_set_up(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        out = self.srs("context", "quiz", cwd=empty).stdout
        self.assertIn("NOT SET UP", out)
        setup = self.srs("context", "setup", cwd=empty).stdout
        self.assertIn("NOT CONFIGURED", setup)
        self.assertIn("NOT A REPOSITORY", setup)
        self.assertIn("default anki", setup)
        self.assertIn("  thirds: ", setup)

    def test_promote_context_filters_by_query(self):
        self.index("## [[Eigendecomposition]]\n- Eigenvectors\n\n## [[Bayes]]\n- Bayes' theorem\n")
        out = self.srs("context", "promote", "bayes").stdout
        self.assertIn("Bayes' theorem", out)
        self.assertNotIn("- Eigenvectors", out)


if __name__ == "__main__":
    unittest.main()
