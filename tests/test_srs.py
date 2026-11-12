"""Tests for Recall's engine (scripts/srs.py) and hook (hooks/guard.py).

Run from the repository root:  python3 -m unittest discover -s tests -v
"""

import datetime as dt
import importlib.util
import json
import os
import re
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

    def test_fuzz_moves_short_intervals_by_a_day(self):
        p = srs.THIRDS_DEFAULTS
        for interval in (3, 8):
            values = {srs.fuzzed(interval, f"c{i}", D("2026-01-01"), p) for i in range(200)}
            self.assertEqual(values, {interval - 1, interval, interval + 1})
        off = dict(p, fuzz=0)
        self.assertEqual({srs.fuzzed(8, f"c{i}", D("2026-01-01"), off) for i in range(50)}, {8})

    def test_concepts_passed_together_spread_after_graduating(self):
        s = sched("thirds")
        dues = set()
        for i in range(30):
            rec = {"phase": "review", "interval": 1, "last": "2026-01-01", "due": "2026-01-02"}
            dues.add(srs.apply_answer(rec, True, D("2026-01-02"), s, f"n{i}")["due"])
        self.assertEqual(dues, {"2026-01-04", "2026-01-05", "2026-01-06"})

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


class RetryTest(unittest.TestCase):
    """A retry: wrong at first, right on a second try. No growth, no lapse."""

    def sched(self, algorithm, interval="same"):
        s = sched(algorithm, fuzz=0 if algorithm == "thirds" else False)
        s["retry"] = {"enabled": True, "interval": interval}
        return s

    def review(self, interval=20, lapses=1):
        return {"phase": "review", "interval": interval, "ease": 2.1, "last": "2026-01-01",
                "due": "2026-01-21", "lapses": lapses}

    def test_review_keeps_interval_ease_and_lapses(self):
        for algorithm in ("anki", "thirds"):
            with self.subTest(algorithm=algorithm):
                rec = srs.apply_answer(self.review(), "retry", D("2026-01-21"), self.sched(algorithm), "X")
                self.assertEqual((rec["phase"], rec["interval"], rec["due"]), ("review", 20, "2026-02-10"))
                self.assertEqual((rec["ease"], rec["lapses"], rec["last"]), (2.1, 1, "2026-01-21"))

    def test_configured_days_cap_the_interval(self):
        rec = srs.apply_answer(self.review(), "retry", D("2026-01-21"), self.sched("thirds", 5), "X")
        self.assertEqual((rec["interval"], rec["due"]), (5, "2026-01-26"))
        rec = srs.apply_answer(self.review(3), "retry", D("2026-01-21"), self.sched("thirds", 5), "X")
        self.assertEqual(rec["interval"], 3)  # never above the current interval

    def test_relearning_repeats_the_step_without_a_lapse(self):
        rec = {"phase": "relearning", "interval": 9, "pending": 3, "due": "2026-01-21", "lapses": 2}
        for algorithm in ("anki", "thirds"):
            with self.subTest(algorithm=algorithm):
                out = srs.apply_answer(rec, "retry", D("2026-01-21"), self.sched(algorithm), "X")
                self.assertEqual((out["phase"], out["pending"], out["lapses"], out["due"]),
                                 ("relearning", 3, 2, "2026-01-22"))

    def test_new_goes_to_learning(self):
        rec = srs.apply_answer(srs.new_record("integrate", D("2026-01-01"), 1), "retry", D("2026-01-02"),
                               self.sched("thirds"), "X")
        self.assertEqual((rec["phase"], rec["due"], rec["introduced"]), ("learning", "2026-01-03", "2026-01-02"))

    def test_hold_is_cleared_and_the_interval_kept(self):
        held = srs.apply_update(self.review(), D("2026-01-20"), "new part")
        rec = srs.apply_answer(held, "retry", D("2026-01-21"), self.sched("anki"), "X")
        self.assertNotIn("hold", rec)
        self.assertEqual((rec["interval"], rec["lapses"]), (20, 1))

    def test_bad_interval_is_refused(self):
        for bad in (0, "long", True, -2):
            with self.subTest(bad=bad), self.assertRaises(srs.SrsError):
                srs.apply_answer(self.review(), "retry", D("2026-01-21"), self.sched("thirds", bad), "X")


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


class NeighborTest(unittest.TestCase):
    """New concepts on adjacent index lines don't start on the same day."""

    INDEX = "## [[Page]]\n- a\n- b\n- c\n- d\n\n## [[Other]]\n- e\n"

    def make(self, concepts):
        return {"version": 1, "next_order": 100, "concepts": concepts, "flags": {"thin": {}}}

    def neighbors(self):
        return srs.index_neighbors(srs.parse_index(self.INDEX))

    def new(self, *names):
        return {n: srs.new_record("integrate", D("2026-01-01"), i) for i, n in enumerate(names)}

    def test_neighbors_are_the_lines_above_and_below_in_the_same_page(self):
        found = self.neighbors()
        self.assertEqual(found["a"], {"b"})
        self.assertEqual(found["b"], {"a", "c"})
        self.assertEqual(found["d"], {"c"})
        self.assertNotIn("e", found)  # alone in its page; d and e are in different pages

    def test_alternate_concepts_start_on_alternate_days(self):
        state = self.make(self.new("a", "b", "c", "d", "e"))
        items, info = srs.daily_list(state, sched(), D("2026-01-10"), self.neighbors())
        self.assertEqual(items, ["a", "c", "e"])
        self.assertEqual(info["carried"], 0)  # waiting new concepts don't block tomorrow's new ones
        self.assertEqual(info["new_waiting"], 2)
        for n in items:
            state["concepts"][n].update(phase="review", introduced="2026-01-10", last="2026-01-10",
                                        due="2026-01-11", interval=1)
        items, _ = srs.daily_list(state, sched(), D("2026-01-10"), self.neighbors())
        self.assertEqual(items, [])  # still the same day: b and d keep waiting
        state["concepts"].update({n: dict(state["concepts"][n], due="2026-01-20") for n in ("a", "c", "e")})
        items, _ = srs.daily_list(state, sched(), D("2026-01-11"), self.neighbors())
        self.assertEqual(items, ["b", "d"])

    def test_a_neighbor_due_for_review_holds_a_new_concept_back(self):
        concepts = self.new("b", "e")
        concepts["a"] = {"phase": "review", "due": "2026-01-10", "interval": 3}
        items, _ = srs.daily_list(self.make(concepts), sched(), D("2026-01-10"), self.neighbors())
        self.assertEqual(items, ["a", "e"])

    def test_reviews_never_wait(self):
        concepts = {n: {"phase": "review", "due": "2026-01-10", "interval": 3} for n in "abcd"}
        items, _ = srs.daily_list(self.make(concepts), sched(), D("2026-01-10"), self.neighbors())
        self.assertEqual(items, ["a", "b", "c", "d"])

    def test_the_freed_slot_goes_to_the_next_new_concept(self):
        s = sched(new_per_day=2)
        items, _ = srs.daily_list(self.make(self.new("a", "b", "e")), s, D("2026-01-10"), self.neighbors())
        self.assertEqual(items, ["a", "e"])

    def test_without_neighbors_the_list_is_unchanged(self):
        items, _ = srs.daily_list(self.make(self.new("a", "b", "c")), sched(), D("2026-01-10"))
        self.assertEqual(items, ["a", "b", "c"])


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


class PaceTest(VaultCase):
    def config(self):
        return json.loads((self.vault / ".srs/config.json").read_text())

    def commits(self):
        return len(self.git("log", "--format=%h").splitlines())

    def queue(self, reviews=0, new=0):
        concepts = {f"r{i:02d}": {"phase": "review", "due": "2026-01-01", "interval": 3} for i in range(reviews)}
        for i in range(new):
            concepts[f"n{i:02d}"] = srs.new_record("integrate", D("2026-01-01"), i)
        state = {"version": 1, "next_order": 99, "concepts": concepts, "flags": {"thin": {}}}
        (self.vault / ".srs/state.json").write_text(json.dumps(state))

    def test_show_changes_nothing(self):
        before = self.commits()
        out = self.srs("config").stdout
        self.assertIn("New concepts per day: 5.", out)
        self.assertIn("together: 20.", out)
        self.assertEqual(self.commits(), before)

    def test_retry_is_off_until_turned_on(self):
        self.assertIn("Retry after a wrong answer: off", self.srs("config").stdout)
        self.assertIn("Retry after a wrong answer: off", self.srs("context", "quiz").stdout)
        before = self.commits()
        out = self.srs("config", "--retry", "on").stdout
        self.assertIn("retry on", out)
        self.assertIn("the interval stays the same", out)
        self.assertEqual(self.config()["scheduling"]["retry"], {"enabled": True, "interval": "same"})
        self.assertEqual(self.commits(), before + 1)
        out = self.srs("config", "--retry-interval", "3").stdout
        self.assertIn("retry interval same -> 3", out)
        self.assertEqual(self.config()["scheduling"]["retry"], {"enabled": True, "interval": 3})
        self.assertIn("the interval becomes 3 days", self.srs("context", "quiz").stdout)
        self.assertIn("Nothing changed.", self.srs("config", "--retry", "on").stdout)
        for bad in ("0", "soon", "-1"):
            self.assertNotEqual(self.srs("config", "--retry-interval", bad, check=False).returncode, 0, bad)
        self.srs("config", "--retry", "off", "--retry-interval", "same")
        self.assertEqual(self.config()["scheduling"]["retry"], {"enabled": False, "interval": "same"})

    def test_set_commits_only_the_config_file(self):
        self.write("Math/Bayes.md", "unrelated edit\n")
        before = self.commits()
        self.srs("config", "--new-per-day", "10", "--daily-limit", "40")
        scheduling = self.config()["scheduling"]
        self.assertEqual((scheduling["new_per_day"], scheduling["daily_limit"]), (10, 40))
        self.assertEqual(self.commits(), before + 1)
        self.assertEqual(self.git("show", "--name-only", "--format=", "HEAD").split(), [".srs/config.json"])
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(),
                         "config: new_per_day 5 -> 10, daily_limit 20 -> 40")

    def test_only_the_named_flag_changes(self):
        self.srs("config", "--new-per-day", "0")
        scheduling = self.config()["scheduling"]
        self.assertEqual((scheduling["new_per_day"], scheduling["daily_limit"]), (0, 20))

    def test_defaults_stay_out_of_a_sparse_file(self):
        path = self.vault / ".srs/config.json"
        path.write_text(json.dumps({"language": "Esperanto"}))
        self.srs("begin", "setup")
        self.srs("end")
        self.srs("config", "--daily-limit", "30")
        self.assertEqual(self.config(), {"language": "Esperanto", "scheduling": {"daily_limit": 30}})

    def test_invalid_values_are_refused_and_write_nothing(self):
        before = (self.vault / ".srs/config.json").read_bytes()
        commits = self.commits()
        for flags in (("--new-per-day", "-1"), ("--daily-limit", "0"), ("--new-per-day", "9", "--daily-limit", "0")):
            self.assertNotEqual(self.srs("config", *flags, check=False).returncode, 0, flags)
        self.assertEqual((self.vault / ".srs/config.json").read_bytes(), before)
        self.assertEqual(self.commits(), commits)

    def test_same_values_make_no_commit(self):
        before = self.commits()
        self.assertIn("Nothing changed.", self.srs("config", "--new-per-day", "5").stdout)
        self.assertEqual(self.commits(), before)

    def test_an_open_session_keeps_its_edits_and_cannot_revert_the_change(self):
        self.srs("begin", "integrate")
        self.edit_via_hook("Math/Bayes.md", "session edit\n")
        self.srs("config", "--daily-limit", "30")
        self.assertEqual(self.git("show", "--name-only", "--format=", "HEAD").split(), [".srs/config.json"])
        session = json.loads((self.vault / ".srs/session.json").read_text())
        self.assertEqual(session["touched"], ["Math/Bayes.md"])
        self.srs("discard")
        self.assertEqual(self.config()["scheduling"]["daily_limit"], 30)
        self.assertNotEqual((self.vault / "Math/Bayes.md").read_text(), "session edit\n")

    def test_new_limit_reaches_the_next_due_list(self):
        self.queue(reviews=25)
        self.assertIn("Carried over to tomorrow: 5.", self.srs("due").stdout)
        self.srs("config", "--daily-limit", "30")
        self.assertIn("Carried over to tomorrow: 0.", self.srs("due").stdout)

    def test_warns_when_reviews_block_new_concepts(self):
        self.queue(reviews=25, new=3)
        self.assertIn("no new concepts enter today", self.srs("config", "--new-per-day", "10").stdout)
        self.queue(reviews=1, new=3)
        self.assertNotIn("no new concepts enter today", self.srs("config").stdout)

    def test_warns_when_the_daily_limit_caps_new_concepts(self):
        self.assertIn("caps new concepts", self.srs("config", "--new-per-day", "21").stdout)

    def test_refused_without_recall(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        self.assertNotEqual(self.srs("config", cwd=outside, check=False).returncode, 0)
        self.assertIn("NOT SET UP", self.srs("context", "config", cwd=outside).stdout)

    def test_skill_can_be_triggered_by_asking(self):
        skill = (ROOT / "skills/config/SKILL.md").read_text()
        self.assertNotIn("disable-model-invocation", skill)
        self.assertIn("context config", skill)


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


class QuizOffTest(VaultCase):
    """With the quiz off, Recall keeps the inbox, the index and the pages; /quiz and /promote don't run."""

    def config(self):
        return json.loads((self.vault / ".srs/config.json").read_text(encoding="utf-8"))

    def save(self, config):
        (self.vault / ".srs/config.json").write_text(json.dumps(config), encoding="utf-8")

    def test_on_by_default_even_without_the_key(self):
        self.assertIs(self.config()["quiz"], True)
        config = self.config()
        del config["quiz"]  # a vault set up before the setting existed
        self.save(config)
        self.assertIn("\nQuiz: on\n", self.srs("context", "integrate").stdout)

    def test_init_takes_it_and_still_prepares_the_schedule(self):
        other = self.tmp / "other"
        other.mkdir()
        (other / "a.md").write_text("# A\n")
        out = self.srs("init", "--git-init", "--quiz", "off", cwd=other).stdout
        self.assertIn("Quiz: off. /config turns it on.", out)
        self.assertNotIn("Scheduling algorithm", out)
        self.assertIs(json.loads((other / ".srs/config.json").read_text())["quiz"], False)
        for name in ("state.json", "log.csv", "quiz-rules.md"):
            self.assertTrue((other / ".srs" / name).is_file(), name)

    def test_setup_context_offers_it(self):
        bare = self.tmp / "bare"
        bare.mkdir()
        out = self.srs("context", "setup", cwd=bare).stdout
        self.assertIn("Quiz (init --quiz on|off; default on)", out)
        self.assertIn("Scheduling algorithms, asked only with the quiz on", out)

    def test_config_turns_it_off_and_on_and_keeps_the_schedule(self):
        self.index("## [[Bayes]]\n- Bayes' theorem\n")
        self.srs("add", "--source", "integrate", "Bayes' theorem")
        before = (self.vault / ".srs/state.json").read_text()
        out = self.srs("config", "--quiz", "off").stdout
        self.assertIn("config: quiz off", out)
        self.assertIn("The schedule is kept as it is (1 concept)", out)
        self.assertNotIn("New concepts per day", out)
        self.assertIn("/integrate review: human", out)
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), "config: quiz off")
        self.assertIs(self.config()["quiz"], False)
        self.assertEqual((self.vault / ".srs/state.json").read_text(), before)
        self.assertIn("Nothing changed.", self.srs("config", "--quiz", "off").stdout)
        out = self.srs("config", "--quiz", "on").stdout
        self.assertIn("config: quiz on", out)
        self.assertIn("Quiz: on", out)
        self.assertIn("New concepts per day", out)

    def test_commands_follow_it(self):
        self.index("## [[Bayes]]\n- Bayes' theorem\n")
        self.srs("config", "--quiz", "off")
        for command in ("quiz", "promote"):
            out = self.srs("context", command).stdout
            self.assertIn("QUIZ OFF", out)
            self.assertNotIn("## Rules", out)
        out = self.srs("context", "integrate").stdout
        self.assertIn("\nQuiz: off\n", out)
        self.assertNotIn("New-concept queue", out)
        out = self.srs("context", "config").stdout
        self.assertIn("Quiz: off.", out)
        self.assertNotIn("Retry after a wrong answer", out)
        self.assertIn("Recall: configured. Quiz: off.", self.srs("context", "setup").stdout)

    def test_an_unknown_value_is_reported_and_config_fixes_it(self):
        config = self.config()
        config["quiz"] = "off"
        self.save(config)
        self.assertIn("ERROR: Unknown quiz value 'off'", self.srs("context", "integrate").stdout)
        self.srs("config", "--quiz", "off")
        self.assertIs(self.config()["quiz"], False)


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
        self.assertEqual(log[1:], ["2026-01-11,Eigenvectors,pass,new,0", "2026-01-11,Bayes' theorem,fail,new,0"])
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


class AmendTest(VaultCase):
    """srs.py amend: correct a contested answer on the same study day."""

    TODAY = "2026-01-10"
    NAME = "Eigenvectors"

    def setUp(self):
        super().setUp()
        self.index("## [[Eigendecomposition]]\n- Eigenvectors\n\n## [[Bayes]]\n- Bayes' theorem\n")
        self.srs("begin", "setup")
        self.srs("end")

    def use(self, algorithm, concepts):
        path = self.vault / ".srs/config.json"
        config = json.loads(path.read_text())
        config.setdefault("scheduling", {})["algorithm"] = algorithm
        path.write_text(json.dumps(config))
        state = {"version": 1, "next_order": 9, "concepts": concepts, "flags": {"thin": {}}}
        (self.vault / ".srs/state.json").write_text(json.dumps(state))
        (self.vault / ".srs/log.csv").unlink(missing_ok=True)

    def state(self, name=None):
        data = json.loads((self.vault / ".srs/state.json").read_text())
        return data["concepts"][name or self.NAME]

    def log_bytes(self):
        return (self.vault / ".srs/log.csv").read_bytes()

    def evolve(self, phase, hold=False, algorithm="anki"):
        """A record in the given phase, built with the real algorithm, and due today."""
        rec = srs.new_record("integrate", D("2026-01-01"), 1)
        answers = {"new": [], "learning": [False], "review": [True, True, True],
                   "relearning": [True, True, True, False]}[phase]
        day = D("2026-01-02")
        for passed in answers:
            rec = srs.apply_answer(rec, passed, day, sched(algorithm), self.NAME)
            day = srs.parse_day(rec["due"])
        self.assertEqual(rec["phase"], phase)
        if hold:
            rec = srs.apply_update(rec, D("2026-01-09"), "new part")
            self.assertTrue(rec["hold"])
        if phase != "new":
            rec["due"] = self.TODAY
        rec["questions"] = ["old one", "old two"]
        return rec

    def test_flip_matches_recording_the_other_result_directly(self):
        cases = [("new", False), ("learning", False), ("review", False), ("relearning", False), ("review", True)]
        for algorithm in ("anki", "thirds"):
            for phase, hold in cases:
                for first, second in (("pass", "fail"), ("fail", "pass")):
                    with self.subTest(algorithm=algorithm, phase=phase, hold=hold, first=first):
                        rec = self.evolve(phase, hold, algorithm)
                        self.use(algorithm, {self.NAME: rec})
                        (self.vault / ".srs/log.csv").unlink(missing_ok=True)
                        self.srs("record", self.NAME, second, "--question", "direct", today=self.TODAY)
                        direct = self.state()
                        direct_log = self.log_bytes()
                        self.use(algorithm, {self.NAME: rec})
                        (self.vault / ".srs/log.csv").unlink(missing_ok=True)
                        self.srs("record", self.NAME, first, "--question", "direct", today=self.TODAY)
                        out = self.srs("amend", self.NAME, second, today=self.TODAY).stdout
                        self.assertEqual(self.state(), direct)
                        self.assertEqual(self.log_bytes(), direct_log)
                        self.assertEqual(self.state()["questions"], ["old one", "old two", "direct"])
                        self.assertIn(self.NAME, out)

    def test_amending_twice_works_from_the_snapshot(self):
        for algorithm in ("anki", "thirds"):
            with self.subTest(algorithm=algorithm):
                self.use(algorithm, {self.NAME: self.evolve("review", algorithm=algorithm)})
                self.srs("record", self.NAME, "fail", today=self.TODAY)
                failed, failed_log = self.state(), self.log_bytes()
                self.srs("amend", self.NAME, "pass", today=self.TODAY)
                passed = self.state()
                self.assertNotEqual(passed["due"], failed["due"])
                self.srs("amend", self.NAME, "fail", today=self.TODAY)
                self.assertEqual(self.state(), failed)
                self.assertEqual(self.log_bytes(), failed_log)
                self.srs("amend", self.NAME, "pass", today=self.TODAY)
                self.assertEqual(self.state(), passed)

    def test_lapses_and_leech_return_to_what_they_were(self):
        for algorithm in ("anki", "thirds"):
            with self.subTest(algorithm=algorithm):
                rec = self.evolve("review", algorithm=algorithm)
                threshold = 8 if algorithm == "anki" else 10
                rec["lapses"] = threshold - 1
                self.use(algorithm, {self.NAME: rec})
                out = self.srs("record", self.NAME, "fail", today=self.TODAY).stdout
                self.assertIn("LEECH", out)
                self.assertEqual(self.state()["lapses"], threshold)
                out = self.srs("amend", self.NAME, "pass", today=self.TODAY).stdout
                self.assertNotIn("LEECH", out)
                self.assertEqual(self.state()["lapses"], threshold - 1)
                self.assertEqual(self.state()["phase"], "review")
                self.assertIn("Score: 1/1", self.srs("today", today=self.TODAY).stdout)
                out = self.srs("amend", self.NAME, "fail", today=self.TODAY).stdout
                self.assertIn("LEECH", out)
                self.assertIn("Leeches: Eigenvectors", self.srs("today", today=self.TODAY).stdout)

    def test_refusals_say_what_to_do(self):
        self.use("anki", {self.NAME: self.evolve("review")})
        refused = self.srs("amend", self.NAME, "pass", today=self.TODAY, check=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("not answered today", refused.stderr)
        self.assertIn("srs.py record", refused.stderr)
        unknown = self.srs("amend", "Nope", "pass", today=self.TODAY, check=False)
        self.assertEqual(unknown.returncode, 2)
        self.srs("record", self.NAME, "pass", today=self.TODAY)
        before = (self.state(), self.log_bytes())
        same = self.srs("amend", self.NAME, "pass", today=self.TODAY, check=False)
        self.assertEqual(same.returncode, 2)
        self.assertIn("already recorded as pass", same.stderr)
        self.assertEqual((self.state(), self.log_bytes()), before)
        # the next study day, the answer is no longer today's
        later = self.srs("amend", self.NAME, "fail", today="2026-01-11", check=False)
        self.assertEqual(later.returncode, 2)
        self.assertIn("not answered today", later.stderr)

    def test_snapshot_from_another_day_is_ignored(self):
        self.use("anki", {self.NAME: self.evolve("review")})
        self.srs("record", self.NAME, "pass", today=self.TODAY)
        path = self.vault / ".srs/state.json"
        data = json.loads(path.read_text())
        data["concepts"][self.NAME]["undo"]["day"] = "2026-01-09"
        path.write_text(json.dumps(data))
        before = (self.state(), self.log_bytes())
        refused = self.srs("amend", self.NAME, "fail", today=self.TODAY, check=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("no snapshot from today", refused.stderr)
        del data["concepts"][self.NAME]["undo"]  # an answer from before 0.7.0
        path.write_text(json.dumps(data))
        refused = self.srs("amend", self.NAME, "fail", today=self.TODAY, check=False)
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(self.log_bytes(), before[1])

    def test_next_record_replaces_the_snapshot(self):
        self.use("anki", {self.NAME: self.evolve("review")})
        self.srs("record", self.NAME, "pass", today=self.TODAY)
        first = self.state()["undo"]
        later = "2026-02-20"
        self.srs("record", self.NAME, "fail", today=later)
        undo = self.state()["undo"]
        self.assertEqual(undo["day"], later)
        self.assertEqual(undo["before"]["last"], self.TODAY)
        self.assertNotIn("undo", undo["before"])
        self.assertNotEqual(undo, first)

    def test_only_the_right_log_line_changes(self):
        rec = self.evolve("review")
        other = self.evolve("review")
        self.use("anki", {self.NAME: rec, "Bayes' theorem": other})
        self.srs("record", "Bayes' theorem", "pass", today=self.TODAY)
        self.srs("record", self.NAME, "pass", today=self.TODAY)
        lines = self.log_bytes().split(b"\r\n")
        self.srs("amend", self.NAME, "fail", today=self.TODAY)
        after = self.log_bytes().split(b"\r\n")
        self.assertEqual(len(after), len(lines))
        changed = [i for i, (a, b) in enumerate(zip(lines, after)) if a != b]
        self.assertEqual(len(changed), 1)
        old, new = lines[changed[0]].split(b","), after[changed[0]].split(b",")
        self.assertEqual(old[:2] + old[3:], new[:2] + new[3:])
        self.assertEqual((old[2], new[2]), (b"pass", b"fail"))
        self.assertEqual(self.state("Bayes' theorem")["phase"], "review")

    def test_marks_state_and_log_as_touched(self):
        self.use("anki", {self.NAME: self.evolve("review")})
        self.srs("begin", "quiz", today=self.TODAY)
        self.srs("record", self.NAME, "fail", today=self.TODAY)
        self.srs("commit", "-m", "quiz: 0/1", today=self.TODAY)
        self.assertEqual(json.loads((self.vault / ".srs/session.json").read_text())["touched"], [])
        self.srs("amend", self.NAME, "pass", today=self.TODAY)
        touched = json.loads((self.vault / ".srs/session.json").read_text())["touched"]
        self.assertEqual(sorted(touched), [".srs/log.csv", ".srs/state.json"])
        self.srs("commit", "-m", "quiz: 1/1", today=self.TODAY)
        self.srs("end", today=self.TODAY)

    def retry_on(self, interval="same"):
        path = self.vault / ".srs/config.json"
        config = json.loads(path.read_text())
        config.setdefault("scheduling", {})["retry"] = {"enabled": True, "interval": interval}
        path.write_text(json.dumps(config))

    def test_retry_is_refused_while_off(self):
        self.use("thirds", {self.NAME: self.evolve("review", algorithm="thirds")})
        before = self.state()
        refused = self.srs("record", self.NAME, "retry", today=self.TODAY, check=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("Retry is off", refused.stderr)
        self.assertEqual(self.state(), before)
        self.srs("record", self.NAME, "pass", today=self.TODAY)
        self.assertEqual(self.srs("amend", self.NAME, "retry", today=self.TODAY, check=False).returncode, 2)

    def test_retry_is_logged_kept_out_of_the_score_and_opens_the_gap(self):
        for algorithm in ("anki", "thirds"):
            with self.subTest(algorithm=algorithm):
                rec = self.evolve("review", algorithm=algorithm)
                self.use(algorithm, {self.NAME: rec})
                self.retry_on()
                self.srs("record", self.NAME, "retry", "--gap", "the missed idea", today=self.TODAY)
                state = self.state()
                self.assertEqual((state["interval"], state["lapses"]), (rec["interval"], rec["lapses"]))
                self.assertEqual(state["gap"]["idea"], "the missed idea")
                self.assertIn(b",retry,review,", self.log_bytes())
                out = self.srs("today", today=self.TODAY).stdout
                self.assertIn("Score: 0/1", out)
                self.assertIn("Retried: Eigenvectors", out)
                self.assertNotIn("Failed", out)

    def test_amend_moves_between_all_three_results(self):
        rec = self.evolve("review", algorithm="thirds")
        self.use("thirds", {self.NAME: rec})
        self.retry_on()
        self.srs("record", self.NAME, "pass", today=self.TODAY)
        self.srs("amend", self.NAME, "retry", today=self.TODAY)
        retried = self.state()
        self.assertEqual((retried["interval"], retried["lapses"]), (rec["interval"], rec["lapses"]))
        self.srs("amend", self.NAME, "fail", today=self.TODAY)
        self.assertEqual(self.state()["lapses"], rec["lapses"] + 1)
        self.srs("amend", self.NAME, "retry", today=self.TODAY)
        self.assertEqual(self.state(), retried)
        self.assertIn(b",retry,", self.log_bytes())

    def test_quiz_skill_tells_how_to_contest(self):
        skill = (ROOT / "skills/quiz/SKILL.md").read_text()
        self.assertIn('srs.py amend "<Concept>" pass', skill)
        self.assertIn("leaves the repeat at the end of the session", skill)
        self.assertIn("pass", self.srs("amend", "--help").stdout)


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

    def test_maintenance_batch_gives_absolute_paths(self):
        self.index("## [[Bayes]]\n- Bayes' theorem\n")
        out = self.srs("context", "maintenance").stdout
        batch = out.split("## Batch to audit")[1]
        self.assertIn("Absolute path: `", batch)
        self.assertIn("/Bayes.md`", batch)


class FolderMapTest(VaultCase):
    MAP = ("# Folder map\n\n| Folder | Takes | Doesn't |\n| :--- | --- | ---: |\n"
           "| **Math** | numbers | code |\n| Gone/ | nothing | |\n{rows}\n## Tie-breaks\n\n- Math or Gone?\n")

    def write_map(self, *rows):
        self.write("folders.md", self.MAP.format(rows="".join(f"| {r} | x | y |\n" for r in rows)))

    def set_placement(self, value):
        config = json.loads((self.vault / ".srs/config.json").read_text())
        config["new_page_placement"] = value
        (self.vault / ".srs/config.json").write_text(json.dumps(config))

    def test_rows_skip_headers_and_separators_of_every_table(self):
        text = self.MAP.format(rows="") + "\n| Other | x |\n|---|---|\n| `Code/` | y |\n"
        self.assertEqual(srs.folder_map_rows(text), ["Math", "Gone", "Code"])

    def test_without_a_map_every_folder_with_pages_is_missing(self):
        (self.vault / "Pictures").mkdir()
        (self.vault / "Pictures/a.png").write_bytes(b"x")
        report = srs_report(self.vault)
        self.assertEqual(report["unmapped_folders"], ["Math"])  # Templates is excluded, Pictures has no pages
        self.assertEqual(report["map_rows_without_folder"], [])
        self.assertIn("Folder map: none (folders.md does not exist", self.srs("context", "integrate").stdout)

    def test_map_rows_are_matched_to_top_level_folders(self):
        (self.vault / "Pictures").mkdir()
        (self.vault / "Pictures/a.png").write_bytes(b"x")
        self.write_map("Pictures", "Lost")
        report = srs_report(self.vault)
        self.assertEqual(report["unmapped_folders"], [])
        self.assertEqual(report["map_rows_without_folder"], ["Gone", "Lost"])  # Pictures exists, without pages
        self.assertNotIn("folders.md", report["unindexed"])  # the map is not a page
        out = self.srs("check").stdout
        self.assertIn("Folder map rows that name no top-level folder (folders.md): 2\n  - Gone\n  - Lost", out)

    def test_flat_placement_has_no_map_checks(self):
        self.set_placement("flat")
        report = srs_report(self.vault)
        self.assertEqual((report["unmapped_folders"], report["map_rows_without_folder"]), ([], []))
        self.assertNotIn("Folder map", self.srs("context", "setup").stdout)

    def test_integrate_context_prints_the_map(self):
        self.write_map()
        out = self.srs("context", "integrate").stdout
        self.assertIn("## Folder map (folders.md): follow it", out)
        self.assertIn("| **Math** | numbers | code |", out)
        self.assertIn("- Math or Gone?", out)

    def test_setup_context_lists_folders_to_map_and_the_template(self):
        out = self.srs("context", "setup").stdout
        self.assertIn("## Folder map: folders.md DOES NOT EXIST", out)
        self.assertIn("  Math/ — 2 pages: Bayes; Eigendecomposition", out)
        self.assertIn("Template:\n# Folder map", out)
        self.assertIn("at least 3 pages", out)
        self.write("Physics/Waves.md", "# Waves\n")
        self.write_map()
        out = self.srs("context", "setup").stdout
        self.assertIn("## Folder map: folders.md has no row for 1 folder", out)
        self.assertIn("  Physics/ — 1 page: Waves", out)
        self.assertNotIn("Template:", out)
        self.write_map("Physics")
        self.assertIn("Folder map: folders.md (every folder with pages has a row).", self.srs("context", "setup").stdout)

    def test_init_takes_the_folder_map_path(self):
        other = self.tmp / "other"
        other.mkdir()
        self.srs("init", "--git-init", "--folder-map", "Meta/Folders.md", cwd=other)
        config = json.loads((other / ".srs/config.json").read_text())
        self.assertEqual(config["paths"]["folder_map"], "Meta/Folders.md")
        self.assertFalse((other / "Meta/Folders.md").exists())  # /setup drafts it


class DiffWarningsTest(VaultCase):
    BASE = "## [[Eigendecomposition]]\n- Eigenvectors\n\n## [[Bayes]]\n- Bayes' theorem\n"

    def committed_index(self, body=None):
        self.index(body or self.BASE)
        self.srs("begin", "setup")  # commits the manual edit, so the index is in HEAD
        self.srs("end")

    def edit_index(self, body):
        self.srs("begin", "maintenance")
        self.edit_via_hook("index.md", "# Index\n\n" + body)

    def test_warns_about_a_long_description_added_in_the_session(self):
        self.committed_index()
        self.edit_index(self.BASE + "- Bayes factor — " + "x" * 70 + "\n")
        out = self.srs("diff").stdout
        self.assertIn("Index warnings", out)
        self.assertIn("Descriptions over 60 characters: 1", out)
        self.assertIn("Bayes factor (70 characters)", out)
        self.assertLess(out.index("Index warnings"), out.index("diff --git"))
        self.srs("commit", "-m", "maintenance: long description")  # a warning, not a refusal
        self.srs("end")

    def test_ignores_problems_on_lines_the_session_did_not_touch(self):
        self.committed_index(self.BASE + "- Bayes factor — " + "x" * 70 + "\n")
        self.edit_index(self.BASE + "- Bayes factor — " + "x" * 70 + "\n- Likelihood\n")
        out = self.srs("diff").stdout
        self.assertNotIn("Index warnings", out)
        self.assertIn("+- Likelihood", out)
        self.srs("abort")

    def test_warns_about_a_name_repeated_against_an_unchanged_line(self):
        self.committed_index()
        self.edit_index(self.BASE.replace("- Bayes' theorem", "- Bayes' theorem\n- Eigenvectors"))
        out = self.srs("diff").stdout
        self.assertIn("Concept names used on more than one page: 1", out)
        self.assertIn("Eigenvectors: [[Eigendecomposition]], [[Bayes]]", out)
        self.srs("abort")

    def test_warns_about_a_link_that_does_not_resolve_and_a_line_not_understood(self):
        self.committed_index()
        self.edit_index(self.BASE + "\n## [[Ghost page]]\n- Ghost\nstray text\n")
        out = self.srs("diff").stdout
        self.assertIn("Index links that don't resolve: 1", out)
        self.assertIn("[[Ghost page]]", out)
        self.assertIn("Index lines not understood: 1", out)
        self.assertIn("stray text", out)
        self.srs("abort")

    def test_a_new_index_is_checked_whole(self):
        self.srs("begin", "setup")
        self.edit_via_hook("index.md", "# Index\n\n" + self.BASE + "- Bayes factor — " + "x" * 70 + "\n")
        self.assertIn("Descriptions over 60 characters: 1", self.srs("diff").stdout)
        self.srs("abort")

    def test_no_warning_section_when_the_index_is_clean_or_unchanged(self):
        self.committed_index()
        self.edit_index(self.BASE + "- Likelihood\n")
        self.assertNotIn("Index warnings", self.srs("diff").stdout)
        self.srs("discard")
        self.edit_via_hook("Math/Bayes.md", "# Bayes\nchanged\n")
        self.assertNotIn("Index warnings", self.srs("diff").stdout)
        self.srs("abort")

    def test_check_still_reports_everything(self):
        self.committed_index(self.BASE + "- Bayes factor — " + "x" * 70 + "\n")
        self.assertIn("Descriptions over 60 characters: 1", self.srs("check").stdout)


class StaleSealsTest(VaultCase):
    INDEX = "## [[Eigendecomposition]]\n- Eigenvectors\n\n## [[Bayes]]\n- Bayes' theorem\n"

    def seals(self):
        return json.loads((self.vault / ".srs/verified.json").read_text())

    def seal_everything_then_orphan_some(self):
        self.write("Math/Gone.md", "# Gone\n")
        self.write("Math/Excluded.md", "# Excluded\n")
        self.index(self.INDEX + "\n## [[Gone]]\n- Gone concept\n\n## [[Excluded]]\n- Excluded concept\n")
        self.srs("verify", "Eigendecomposition", "Bayes", "Gone", "Excluded")
        (self.vault / "Math/Gone.md").unlink()
        cfg = json.loads((self.vault / ".srs/config.json").read_text())
        cfg["exclude"].append("Math/Excluded.md")
        (self.vault / ".srs/config.json").write_text(json.dumps(cfg))

    def test_check_reports_seals_of_missing_and_excluded_files(self):
        self.seal_everything_then_orphan_some()
        self.assertEqual(srs_report(self.vault)["stale_seals"], ["Math/Excluded.md", "Math/Gone.md"])
        out = self.srs("check").stdout
        self.assertIn("Stale seals", out)
        self.assertIn("  - Math/Gone.md", out)

    def test_check_reports_seals_of_the_inbox_and_the_index(self):
        self.index(self.INDEX)
        (self.vault / ".srs/verified.json").write_text(json.dumps({
            "inbox.md": {"blob": "x", "entry": "y", "day": "2026-01-10"},
            "index.md": {"blob": "x", "entry": "y", "day": "2026-01-10"},
            "Math/Bayes.md": {"blob": "x", "entry": "y", "day": "2026-01-10"}}))
        self.assertEqual(srs_report(self.vault)["stale_seals"], ["inbox.md", "index.md"])

    def test_check_has_nothing_to_report_when_every_seal_has_a_page(self):
        self.index(self.INDEX)
        self.srs("verify", "Bayes")
        self.assertEqual(srs_report(self.vault)["stale_seals"], [])
        self.assertIn("Stale seals (", self.srs("check").stdout)
        self.assertIn("): none", self.srs("check").stdout)

    def test_prune_removes_only_stale_seals_inside_the_session(self):
        self.seal_everything_then_orphan_some()
        self.srs("begin", "maintenance")
        out = self.srs("prune-seals").stdout
        self.assertIn("Removed 2 stale seals: Math/Excluded.md, Math/Gone.md", out)
        self.assertEqual(sorted(self.seals()), ["Math/Bayes.md", "Math/Eigendecomposition.md"])
        self.assertIn(".srs/verified.json", self.srs("diff").stdout)
        self.srs("commit", "-m", "maintenance: 2 stale seals removed")
        self.srs("end")
        self.assertEqual(srs_report(self.vault)["stale_seals"], [])

    def test_prune_with_nothing_stale_changes_nothing(self):
        self.index(self.INDEX)
        self.srs("verify", "Bayes")
        self.srs("begin", "maintenance")
        before = (self.vault / ".srs/verified.json").read_text()
        self.assertEqual(self.srs("prune-seals").stdout.strip(), "Removed 0 stale seals: -")
        self.assertEqual((self.vault / ".srs/verified.json").read_text(), before)
        self.assertEqual(self.srs("diff").stdout.strip(), "No changes to review.")
        self.srs("end")

    def test_maintenance_context_lists_the_stale_seals(self):
        self.seal_everything_then_orphan_some()
        self.assertIn("  - Math/Gone.md", self.srs("context", "maintenance").stdout)


class EmptyCommitTest(VaultCase):
    def test_commit_with_nothing_to_commit_is_neutral_and_succeeds(self):
        self.srs("begin", "maintenance")
        proc = self.srs("commit", "-m", "maintenance: 0 pages verified", check=False)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "Nothing to commit.")
        self.assertEqual(proc.stderr, "")
        self.srs("end")
        self.assertNotIn("maintenance: 0 pages verified", self.git("log", "--format=%s"))


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

    def test_session_commits_a_note_removed_with_rm(self):
        self.write("Math/Old.md", "old\n")
        self.srs("begin", "integrate")
        bash = lambda cmd: self.guard({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(self.vault)})
        self.assertIsNone(bash('rm "Math/Old.md"'))
        (self.vault / "Math/Old.md").unlink()
        self.assertIn("Committed", self.srs("commit", "-m", "integrate: drop page").stdout)
        self.srs("end")
        self.assertEqual(self.git("status", "--short"), "")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "integrate: drop page\n")

    def test_rm_is_not_recorded_with_operators_or_outside_notes(self):
        self.write("Math/Keep.md", "keep\n")
        self.srs("begin", "integrate")
        bash = lambda cmd: self.guard({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(self.vault)})
        bash('ls && rm Math/Keep.md')
        bash('rm data.csv .obsidian/app.json')
        session = json.loads((self.vault / ".srs/session.json").read_text())
        self.assertEqual(session["touched"], [])
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


class GapTest(VaultCase):
    """A fail opens a gap on the concept: its questions target the idea until a pass after some days."""

    DAY = "2026-01-11"

    def setUp(self):
        super().setUp()
        # A line between the two keeps them from being index neighbors, which would start them on different days.
        self.index("## [[Eigendecomposition]]\n- Eigenvectors\n- Spectrum\n- Eigenvalues\n")
        self.srs("add", "--source", "integrate", "Eigenvectors")
        self.srs("record", "Eigenvectors", "fail", "--gap", "Av = λv defines them", today=self.DAY)

    def record(self):
        return json.loads((self.vault / ".srs/state.json").read_text(encoding="utf-8"))["concepts"]["Eigenvectors"]

    def answer(self, result, today, *extra):
        return self.srs("record", "Eigenvectors", result, *extra, today=today).stdout

    def set_record(self, **values):
        path = self.vault / ".srs/state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["concepts"]["Eigenvectors"].update(values)
        path.write_text(json.dumps(state), encoding="utf-8")

    def test_a_fail_opens_the_gap_and_the_list_shows_it(self):
        self.assertEqual(self.record()["gap"], {"idea": "Av = λv defines them", "fails": 1})
        due = self.srs("due", today="2026-01-12").stdout
        self.assertIn("Gap (ask about this idea, not a new angle): Av = λv defines them", due)

    def test_a_pass_closes_it_only_after_enough_days(self):
        self.assertNotIn("Gap closed", self.answer("pass", "2026-01-12"))  # 1 day since the fail
        self.assertNotIn("Gap closed", self.answer("pass", "2026-01-13"))  # 1 day
        self.assertIn("gap", self.record())
        self.set_record(due="2026-01-16")  # the algorithm's fuzz moves the real date by a day
        self.assertIn("Gap closed: Av = λv defines them", self.answer("pass", "2026-01-16"))  # 3 days
        self.assertNotIn("gap", self.record())
        self.assertNotIn("Gap", self.srs("due", today="2026-02-20").stdout)

    def test_a_late_first_pass_closes_it(self):
        self.assertIn("Gap closed", self.answer("pass", "2026-01-14"))

    def test_a_fail_keeps_the_idea_and_counts(self):
        self.answer("fail", "2026-01-12", "--gap", "a reworded idea")
        self.assertEqual(self.record()["gap"], {"idea": "Av = λv defines them", "fails": 2})

    def test_the_dates_are_the_same_as_without_a_gap(self):
        self.answer("pass", "2026-01-12")
        with_gap = self.record()
        self.srs("gap", "Eigenvectors", "--close", today="2026-01-12")
        self.assertEqual(self.record()["due"], with_gap["due"])
        self.srs("add", "--source", "integrate", "Eigenvalues", today=self.DAY)
        self.srs("record", "Eigenvalues", "fail", today="2026-01-12")
        self.srs("record", "Eigenvalues", "pass", today="2026-01-13")
        other = json.loads((self.vault / ".srs/state.json").read_text(encoding="utf-8"))["concepts"]["Eigenvalues"]
        self.assertEqual((other["phase"], other["interval"]), (with_gap["phase"], with_gap["interval"]))

    def test_a_fail_without_an_idea_asks_for_it(self):
        self.srs("add", "--source", "integrate", "Eigenvalues", today=self.DAY)
        out = self.srs("record", "Eigenvalues", "fail", today="2026-01-12").stdout
        self.assertIn("No gap opened", out)

    def test_a_question_on_new_content_does_not_test_the_gap(self):
        self.set_record(phase="review", interval=8, due="2026-01-20", last="2026-01-12", focus="new part")
        due = self.srs("due", today="2026-01-20").stdout
        self.assertIn("Focus (new content): new part", due)
        self.assertNotIn("Gap (", due)
        self.assertNotIn("Gap closed", self.answer("pass", "2026-01-20"))
        self.assertEqual(self.record()["gap"]["fails"], 1)
        self.assertIsNone(self.record()["focus"])

    def test_a_failed_focus_replaces_the_gap_with_its_idea(self):
        self.set_record(phase="review", interval=8, due="2026-01-20", last="2026-01-12", focus="new part")
        self.answer("fail", "2026-01-20", "--gap", "the new part")
        self.assertEqual(self.record()["gap"], {"idea": "the new part", "fails": 1})

    def test_amend_to_pass_removes_the_gap_of_that_fail(self):
        self.srs("amend", "Eigenvectors", "pass", today=self.DAY)
        self.assertNotIn("gap", self.record())

    def test_amend_to_fail_opens_the_gap(self):
        self.srs("add", "--source", "integrate", "Eigenvalues", today=self.DAY)
        self.srs("record", "Eigenvalues", "pass", today="2026-01-12")
        self.srs("amend", "Eigenvalues", "fail", "--gap", "the idea", today="2026-01-12")
        other = json.loads((self.vault / ".srs/state.json").read_text(encoding="utf-8"))["concepts"]["Eigenvalues"]
        self.assertEqual(other["gap"], {"idea": "the idea", "fails": 1})

    def test_amend_can_undo_a_closed_gap(self):
        self.answer("pass", "2026-01-14")
        self.assertNotIn("gap", self.record())
        self.srs("amend", "Eigenvectors", "fail", today="2026-01-14")
        self.assertEqual(self.record()["gap"]["fails"], 2)

    def test_opening_and_closing_by_hand(self):
        self.assertIn("gap closed", self.srs("gap", "Eigenvectors", "--close").stdout)
        self.assertNotIn("gap", self.record())
        self.assertIn("no open gap", self.srs("gap", "Eigenvectors", "--close").stdout)
        self.assertIn("gap open", self.srs("gap", "Eigenvectors", "another idea").stdout)
        self.assertEqual(self.record()["gap"], {"idea": "another idea", "fails": 1})

    def test_the_gap_command_needs_one_of_idea_or_close(self):
        self.assertEqual(self.srs("gap", "Eigenvectors", check=False).returncode, 1)
        self.assertEqual(self.srs("gap", "Eigenvectors", "idea", "--close", check=False).returncode, 1)
        self.assertEqual(self.srs("gap", "Nope", "idea", check=False).returncode, 2)

    def test_a_rename_carries_the_gap(self):
        self.index("## [[Eigendecomposition]]\n- Eigenvector\n- Eigenvalues\n")
        self.srs("rename", "Eigenvectors", "Eigenvector")
        state = json.loads((self.vault / ".srs/state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["concepts"]["Eigenvector"]["gap"]["idea"], "Av = λv defines them")

    def test_maintenance_reports_only_gaps_that_wont_close(self):
        self.assertIn("Gaps that won't close (3+ fails): none", self.srs("check").stdout)
        self.answer("fail", "2026-01-12")
        self.answer("fail", "2026-01-13")
        self.assertIn("Eigenvectors: Av = λv defines them (3 fails)", self.srs("check").stdout)

    def test_stats_count_open_gaps(self):
        self.assertIn("Open gaps: 1", self.srs("stats").stdout)

    def test_a_record_from_an_older_version_still_works(self):
        self.set_record(gap=None)
        self.assertNotIn("Gap (", self.srs("due", today="2026-01-12").stdout)
        self.answer("pass", "2026-01-12")

    def test_the_quiz_skill_targets_the_gap(self):
        skill = (ROOT / "skills/quiz/SKILL.md").read_text()
        self.assertIn('--gap "<the idea the answer missed', skill)
        self.assertIn("Test the idea the answer missed", skill)
        self.assertNotIn("from another angle", skill)
        self.assertIn('srs.py gap "<Concept>" --close', skill)
        self.assertIn("idea the gap names", (ROOT / "templates/quiz-rules.md").read_text())
        self.assertIn("gaps that won't close", (ROOT / "skills/maintenance/SKILL.md").read_text())


class SuggestionsTest(VaultCase):
    """/quiz turns suggested replies off in the vault, since they can show the answer.

    Claude Code reads the setting only at startup, so the change helps from the next session on
    and stays: restoring it at the end would undo it before any quiz could benefit.
    """

    def setUp(self):
        super().setUp()
        self.env.pop("CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION", None)
        self.settings = self.vault / ".claude/settings.local.json"

    def read_settings(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def test_quiz_turns_them_off_and_they_stay_off(self):
        self.srs("begin", "quiz")
        self.assertIs(self.read_settings()["promptSuggestionEnabled"], False)
        self.srs("end")
        self.assertIs(self.read_settings()["promptSuggestionEnabled"], False)

    def test_other_commands_leave_them_alone(self):
        self.srs("begin", "integrate")
        self.assertFalse(self.settings.exists())
        self.srs("end")

    def test_other_keys_are_kept_in_order(self):
        self.write(".claude/settings.local.json", '{"z": 1, "promptSuggestionEnabled": true, "a": 2}\n')
        self.srs("begin", "quiz")
        self.srs("end")
        self.assertEqual(list(self.read_settings().items()), [("z", 1), ("promptSuggestionEnabled", False), ("a", 2)])

    def test_turning_them_off_says_it_applies_from_the_next_session(self):
        out = self.srs("begin", "quiz").stdout
        self.assertIn("only when it starts", out)
        self.assertIn("/config", out)
        self.srs("end")

    def test_already_off_needs_no_note(self):
        self.write(".claude/settings.local.json", '{"promptSuggestionEnabled": false}\n')
        self.assertNotIn("Tell the user", self.srs("begin", "quiz").stdout)
        self.srs("end")
        self.assertIs(self.read_settings()["promptSuggestionEnabled"], False)

    def test_abort_leaves_them_off(self):
        self.srs("begin", "quiz")
        self.srs("abort")
        self.assertIs(self.read_settings()["promptSuggestionEnabled"], False)

    def assert_only_the_note_is_committed(self):
        self.write(".claude/settings.local.json", '{"a": 1}\n')
        self.write("Math/Bayes.md", "edited again\n")
        self.srs("begin", "quiz")
        files = self.git("show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(files, ["Math/Bayes.md"])
        self.assertNotIn("Edits outside Recall", self.srs("status").stdout)
        self.srs("end")

    def test_the_file_is_never_committed_when_git_does_not_ignore_it(self):
        self.git("config", "core.excludesFile", os.devnull)
        self.assert_only_the_note_is_committed()

    def test_the_file_is_never_committed_when_git_ignores_it(self):
        self.write(".gitignore", (self.vault / ".gitignore").read_text() + ".claude/settings.local.json\n")
        self.git("add", ".gitignore")
        self.git("commit", "-q", "-m", "ignore local settings")
        self.assert_only_the_note_is_committed()

    def test_invalid_settings_are_kept_and_the_user_is_told(self):
        self.write(".claude/settings.local.json", "not json\n")
        out = self.srs("begin", "quiz").stdout
        self.assertIn("Tell the user", out)
        self.assertIn("/config", out)
        self.srs("end")
        self.assertEqual(self.settings.read_text(), "not json\n")

    def test_the_environment_variable_gets_an_explanation(self):
        self.env["CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION"] = "true"
        out = self.srs("begin", "quiz").stdout
        self.assertIn("can show the answer", out)
        self.assertIn("turn it off where it is defined", out)
        self.srs("end")

    def test_no_variable_note_without_the_variable(self):
        self.assertNotIn("turn it off where it is defined", self.srs("begin", "quiz").stdout)
        self.srs("end")


class DurationTest(VaultCase):
    """Closing a quiz reports how long it lasted."""

    def test_end_reports_the_quiz_duration(self):
        self.srs("begin", "quiz")
        path = self.vault / ".srs/session.json"
        session = json.loads(path.read_text(encoding="utf-8"))
        started = dt.datetime.now() - dt.timedelta(minutes=21, seconds=10)
        session["started"] = started.replace(microsecond=0).isoformat()
        path.write_text(json.dumps(session), encoding="utf-8")
        self.assertIn("closed. Duration: 21 min.", self.srs("end").stdout)

    def test_no_duration_for_other_commands(self):
        self.srs("begin", "integrate")
        self.assertNotIn("Duration", self.srs("end").stdout)


class QuestionCountTest(VaultCase):
    """The quiz's denominator is the day's real total, capped by the daily limit."""

    DAY = "2026-01-11"

    def setUp(self):
        super().setUp()
        self.index("## [[Eigendecomposition]]\n- Eigenvectors\n- Spectrum\n- Eigenvalues\n\n## [[Bayes]]\n- Bayes' theorem\n")
        self.srs("add", "--source", "integrate", "Eigenvectors", "Eigenvalues", "Bayes' theorem")

    def due(self):
        return self.srs("due", today=self.DAY).stdout

    def set_daily_limit(self, limit):
        path = self.vault / ".srs/config.json"
        config = json.loads(path.read_text())
        config["scheduling"]["daily_limit"] = limit
        path.write_text(json.dumps(config))

    def test_denominator_is_the_total_and_stays_through_the_day(self):
        self.assertIn("Question count: start at 1/3, end at 3/3.", self.due())
        self.srs("record", "Eigenvectors", "fail", today=self.DAY)
        self.assertIn("Question count: start at 2/3, end at 3/3.", self.due())
        self.srs("record", "Eigenvalues", "pass", today=self.DAY)
        self.assertIn("Question count: start at 3/3, end at 3/3.", self.due())

    def test_index_neighbors_start_on_different_days(self):
        self.index("## [[Eigendecomposition]]\n- Eigenvectors\n- Eigenvalues\n\n## [[Bayes]]\n- Bayes' theorem\n")
        due = self.due()
        self.assertIn("Question count: start at 1/2, end at 2/2.", due)
        self.assertNotIn("Eigenvalues", due)
        out = self.srs("record", "Eigenvalues", "pass", today=self.DAY, check=False)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("Next question: 2/2.", self.srs("record", "Eigenvectors", "pass", today=self.DAY).stdout)
        # The next day Eigenvectors is back for its 1-day review, so Eigenvalues waits once more.
        self.assertNotIn("Eigenvalues", self.srs("due", today="2026-01-12").stdout)
        self.srs("record", "Eigenvectors", "pass", today="2026-01-12")
        self.assertIn("Eigenvalues", self.srs("due", today="2026-01-13").stdout)

    def test_record_gives_the_next_count(self):
        self.assertIn("Next question: 2/3.", self.srs("record", "Eigenvectors", "fail", today=self.DAY).stdout)
        self.assertIn("Next question: 3/3.", self.srs("record", "Eigenvalues", "pass", today=self.DAY).stdout)
        out = self.srs("record", "Bayes' theorem", "pass", today=self.DAY).stdout
        self.assertIn("That was the last question (3/3).", out)
        self.assertNotIn("Next question", out)

    def test_denominator_is_capped_by_the_daily_limit(self):
        self.set_daily_limit(2)
        self.assertIn("Question count: start at 1/2, end at 2/2.", self.due())

    def test_no_count_when_nothing_is_left_to_ask(self):
        for name in ("Eigenvectors", "Eigenvalues", "Bayes' theorem"):
            self.srs("record", name, "pass", today=self.DAY)
        out = self.due()
        self.assertIn("Nothing to ask today", out)
        self.assertNotIn("Question count", out)

    def test_no_preparing_ahead_for_a_short_list(self):
        self.assertNotIn("Prepare ahead", self.due())

    def test_quiz_context_and_skill_use_the_count(self):
        self.assertIn("Question count: start at 1/3", self.srs("context", "quiz", today=self.DAY).stdout)
        skill = (ROOT / "skills/quiz/SKILL.md").read_text()
        self.assertIn("Question count", skill)
        self.assertIn("without a count", skill)


class PrepareAheadTest(VaultCase):
    """More than 10 questions left: /quiz has a fork draft them from the fourth item on."""

    DAY = "2026-01-11"
    NAMES =[f"Concept {n}" for n in range(1, 13)]

    def setUp(self):
        super().setUp()
        self.index("## [[Big page]]\n" + "".join(f"- {name}\n- Note on {name}\n" for name in self.NAMES))
        path = self.vault / ".srs/config.json"
        config = json.loads(path.read_text())
        config["scheduling"]["new_per_day"] = 12
        path.write_text(json.dumps(config))
        self.srs("add", "--source", "integrate", *self.NAMES)

    def due(self):
        return self.srs("due", today=self.DAY).stdout

    def test_long_list_is_prepared_ahead_from_the_fourth_item(self):
        self.assertIn("Prepare ahead: items 4 to 12 of the list below (questions 4/12 to 12/12).", self.due())
        self.assertIn("Prepare ahead", self.srs("context", "quiz", today=self.DAY).stdout)

    def test_range_follows_what_is_left(self):
        self.srs("record", "Concept 1", "pass", today=self.DAY)
        self.assertIn("Prepare ahead: items 4 to 11 of the list below (questions 5/12 to 12/12).", self.due())

    def test_ten_or_fewer_left_are_not_prepared(self):
        self.srs("record", "Concept 1", "pass", today=self.DAY)
        self.srs("record", "Concept 2", "pass", today=self.DAY)
        self.assertNotIn("Prepare ahead", self.due())

    def test_skill_launches_a_fork(self):
        skill = (ROOT / "skills/quiz/SKILL.md").read_text()
        self.assertIn("Prepare ahead", skill)
        self.assertIn('"fork"', skill)
        self.assertIn("say nothing and end your turn at once", skill)


class WelcomeTest(VaultCase):
    """The SessionStart message: `srs.py welcome`, read-only, silent on any problem."""

    DAY = "2026-01-11"
    NAMES = ("Eigenvectors", "Eigenvalues", "Bayes' theorem")

    def setUp(self):
        super().setUp()
        self.index("## [[Eigendecomposition]]\n- Eigenvectors\n- Spectrum\n- Eigenvalues\n\n## [[Bayes]]\n- Bayes' theorem\n"
                   "- Prior\n- Fresh\n- Posterior\n- Other\n")
        self.set_config(language="English")
        self.write("inbox.md", "")

    def set_config(self, **values):
        path = self.vault / ".srs/config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config.update(values)
        path.write_text(json.dumps(config), encoding="utf-8")

    def add(self, *names, today="2026-01-10"):
        self.srs("add", "--source", "integrate", *names, today=today)

    def run_welcome(self, stdin=None, cwd=None, env_extra=None, today=None):
        payload = json.dumps({"cwd": str(self.vault)}) if stdin is None else stdin
        env = {k: v for k, v in self.env.items() if k != "CLAUDE_PROJECT_DIR"}
        env["RECALL_TODAY"] = today or self.DAY
        env.update(env_extra or {})
        return subprocess.run([sys.executable, str(SRS), "welcome"], input=payload.encode("utf-8"),
                              cwd=str(cwd or self.tmp), env=env, capture_output=True)

    def welcome(self, **kwargs):
        proc = self.run_welcome(**kwargs)
        self.assertEqual((proc.returncode, proc.stderr), (0, b""))
        message = json.loads(proc.stdout.decode("utf-8"))["systemMessage"]
        self.assertTrue(message.startswith("\nRecall: "), message)  # apart from the host's label
        return message[1:]

    def assertSilent(self, proc):
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"", b""))

    def test_new_concepts_with_quiz_line(self):
        self.add(*self.NAMES)
        self.assertEqual(self.welcome(), "Recall: today 3 questions: 3 new concepts\n  /quiz        take today's quiz")

    def test_quiz_off_shows_only_the_commands_worth_running(self):
        self.add(*self.NAMES)
        self.set_config(quiz=False)
        self.assertSilent(self.run_welcome())
        self.write("inbox.md", "## Fração\ntext\n")
        message = json.loads(self.run_welcome().stdout.decode("utf-8"))["systemMessage"]
        self.assertEqual(message, "\nRecall:\n  /integrate   1 block in the inbox: Fração")
        (self.vault / ".srs/session.json").write_text("{}", encoding="utf-8")
        message = json.loads(self.run_welcome().stdout.decode("utf-8"))["systemMessage"]
        self.assertEqual(message, "\nRecall: interrupted session! Run a Recall command to recover it.\n"
                                  "  /integrate   1 block in the inbox: Fração")

    def test_reviews_count_learning_relearning_updated_and_reviews(self):
        self.add(*self.NAMES)
        self.srs("record", "Eigenvectors", "fail", today=self.DAY)  # learning
        self.srs("record", "Eigenvalues", "pass", today=self.DAY)  # review
        self.srs("record", "Bayes' theorem", "pass", today=self.DAY)
        self.srs("update", "Bayes' theorem", "--focus", "new part", today=self.DAY)  # on hold
        self.add("Fresh", today=self.DAY)
        self.assertEqual(self.welcome(today="2026-01-12").splitlines()[0],
                         "Recall: today 4 questions: 3 reviews, 1 new concept")

    def test_singular_forms_in_english(self):
        self.add("Eigenvectors")
        self.assertEqual(self.welcome().splitlines()[0], "Recall: today 1 question: 1 new concept")
        self.srs("record", "Eigenvectors", "fail", today=self.DAY)
        self.assertEqual(self.welcome(today="2026-01-12").splitlines()[0], "Recall: today 1 question: 1 review")

    def test_singular_forms_in_portuguese(self):
        self.set_config(language="Português")
        self.add("Eigenvectors")
        self.assertEqual(self.welcome().splitlines()[0], "Recall: hoje 1 pergunta: 1 concept novo")
        self.srs("record", "Eigenvectors", "fail", today=self.DAY)
        self.assertEqual(self.welcome(today="2026-01-12").splitlines()[0], "Recall: hoje 1 pergunta: 1 revisão")

    def test_plurals_and_zero_parts_in_portuguese(self):
        self.set_config(language="pt-BR")
        self.add(*self.NAMES)
        for name in self.NAMES:
            self.srs("record", name, "fail", today=self.DAY)
        self.add("Fresh", "Other", today=self.DAY)
        self.assertEqual(self.welcome(today="2026-01-12"),
                         "Recall: hoje 5 perguntas: 3 revisões, 2 concepts novos\n"
                         "  /quiz        fazer o quiz de hoje")
        self.set_config(language="Portuguese")
        self.srs("remove", "Fresh", today="2026-01-12")
        self.srs("remove", "Other", today="2026-01-12")
        self.assertEqual(self.welcome(today="2026-01-12").splitlines()[0], "Recall: hoje 3 perguntas: 3 revisões")

    def test_language_choice(self):
        for value, expected in (("Português", "pt"), ("PORTUGUESE", "pt"), ("pt", "pt"), ("PT-BR", "pt"),
                                (" portugues ", "pt"), ("Brazilian Portuguese", "pt"), ("português do Brasil", "pt"),
                                ("English", "en"), ("Español", "en"), ("", "en"),
                                (None, "en"), (5, "en")):
            self.assertIs(srs.welcome_strings(value), srs.WELCOME_STRINGS[expected], value)
        self.assertEqual(srs.WELCOME_STRINGS["pt"].keys(), srs.WELCOME_STRINGS["en"].keys())

    def test_partly_done_shows_what_remains(self):
        self.add(*self.NAMES)
        self.srs("record", "Eigenvectors", "pass", today=self.DAY)
        self.assertEqual(self.welcome(), "Recall: 2 questions left today: 2 new concepts\n"
                                         "  /quiz        continue today's quiz")
        self.srs("record", "Eigenvalues", "pass", today=self.DAY)
        self.assertEqual(self.welcome().splitlines()[0], "Recall: 1 question left today: 1 new concept")
        self.set_config(language="pt")
        self.assertEqual(self.welcome(), "Recall: hoje falta 1 pergunta: 1 concept novo\n"
                                         "  /quiz        continuar o quiz de hoje")

    def test_quiz_done_shows_the_score(self):
        self.add(*self.NAMES)
        self.srs("record", "Eigenvectors", "fail", today=self.DAY)
        self.srs("record", "Eigenvalues", "pass", today=self.DAY)
        self.srs("record", "Bayes' theorem", "pass", today=self.DAY)
        self.assertEqual(self.welcome(), "Recall: today's quiz done: 2/3")
        self.set_config(language="Português")
        self.assertEqual(self.welcome(), "Recall: quiz de hoje feito: 2/3")

    def test_nothing_to_ask_says_when_the_next_review_is(self):
        self.add("Eigenvectors")
        self.srs("record", "Eigenvectors", "pass", today=self.DAY)  # due 2026-01-12
        self.assertEqual(self.welcome(today="2026-01-11"), "Recall: today's quiz done: 1/1")
        state = json.loads((self.vault / ".srs/state.json").read_text(encoding="utf-8"))
        state["concepts"]["Eigenvectors"].update(due="2026-01-20", last="2026-01-05")
        (self.vault / ".srs/state.json").write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(self.welcome(today="2026-01-19"), "Recall: nothing to ask today. Next quiz tomorrow")
        self.assertEqual(self.welcome(today="2026-01-15"), "Recall: nothing to ask today. Next quiz in 5 days")
        self.set_config(language="Português")
        self.assertEqual(self.welcome(today="2026-01-19"), "Recall: nada para hoje. Próximo quiz amanhã")
        self.assertEqual(self.welcome(today="2026-01-15"), "Recall: nada para hoje. Próximo quiz em 5 dias")

    def set_pace(self, **values):
        config = json.loads((self.vault / ".srs/config.json").read_text(encoding="utf-8"))
        config["scheduling"].update(values)
        self.set_config(scheduling=config["scheduling"])

    def test_a_queue_at_pace_zero_never_promises_a_quiz(self):
        self.add("Eigenvectors", "Eigenvalues")
        self.set_pace(new_per_day=0)
        self.assertEqual(self.welcome(), "Recall: nothing to ask today. 2 concepts waiting, but the daily pace is 0 "
                                         "(change it with /config)")
        self.set_config(language="pt")
        self.assertEqual(self.welcome(), "Recall: nada para hoje. 2 concepts aguardando, mas o ritmo diário está em 0 "
                                         "(mude com /config)")
        self.assertIn("nothing will come at this pace", self.srs("due", today=self.DAY).stdout)

    def test_pace_zero_still_promises_the_reviews_that_will_come(self):
        self.add("Eigenvectors", "Eigenvalues")
        self.srs("record", "Eigenvectors", "pass", today=self.DAY)  # due 2026-01-12
        self.set_pace(new_per_day=0)
        state = json.loads((self.vault / ".srs/state.json").read_text(encoding="utf-8"))
        state["concepts"]["Eigenvectors"].update(due="2026-01-20")
        (self.vault / ".srs/state.json").write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(self.welcome(today="2026-01-15"), "Recall: nothing to ask today. Next quiz in 5 days")

    def test_nothing_scheduled(self):
        self.assertEqual(self.welcome(), "Recall: no reviews scheduled")
        self.set_config(language="pt")
        self.assertEqual(self.welcome(), "Recall: nenhuma revisão agendada")

    def seal_pages(self, count, day="2026-01-01"):
        names = [f"P{n}" for n in range(1, count + 1)]
        for name in names:
            self.write(f"Pages/{name}.md", f"# {name}\ntext\n")
        self.index("".join(f"## [[{name}]]\n- Concept {name}\n\n" for name in names))
        self.srs("verify", *names, today=day)
        return names

    def change_pages(self, names):
        for name in names:
            self.write(f"Pages/{name}.md", f"# {name}\nchanged\n")

    def maintenance_line(self, **kwargs):
        lines = [l for l in self.welcome(**kwargs).splitlines() if "/maintenance" in l]
        return lines[0] if lines else None

    def test_sealed_pages_ask_for_nothing(self):
        self.seal_pages(6)
        self.assertIsNone(self.maintenance_line())

    def test_few_changes_recently_stay_quiet(self):
        names = self.seal_pages(6)
        self.change_pages(names[:4])
        self.assertIsNone(self.maintenance_line(today="2026-01-14"))

    def test_five_changed_pages_suggest_it_with_the_last_check(self):
        names = self.seal_pages(6)
        self.change_pages(names[:5])
        self.assertEqual(self.maintenance_line(),
                         "  /maintenance 5 pages changed since the last check (10 days ago)")
        self.assertIn("(1 day ago)", self.maintenance_line(today="2026-01-02"))
        self.assertIn("(today)", self.maintenance_line(today="2026-01-01"))
        self.set_config(language="pt")
        self.assertEqual(self.maintenance_line(),
                         "  /maintenance 5 páginas mudaram desde a última verificação (há 10 dias)")
        self.assertIn("(há 1 dia)", self.maintenance_line(today="2026-01-02"))
        self.assertIn("(hoje)", self.maintenance_line(today="2026-01-01"))

    def test_one_changed_page_suggests_it_after_two_weeks(self):
        names = self.seal_pages(2)
        self.change_pages(names[:1])
        self.assertIsNone(self.maintenance_line(today="2026-01-14"))  # 13 days
        self.assertEqual(self.maintenance_line(today="2026-01-15"),
                         "  /maintenance 1 page changed since the last check (14 days ago)")
        self.set_config(language="pt")
        self.assertIn("1 página mudou desde", self.maintenance_line(today="2026-01-15"))

    def test_two_weeks_without_changes_ask_for_nothing(self):
        self.seal_pages(2)
        self.assertIsNone(self.maintenance_line(today="2026-03-01"))

    def test_a_new_page_without_a_seal_counts(self):
        names = self.seal_pages(1)
        self.write("Pages/New.md", "# New\n")
        self.index(f"## [[{names[0]}]]\n- Concept {names[0]}\n\n## [[New]]\n- Fresh idea\n")
        self.assertIn("1 page changed", self.maintenance_line(today="2026-01-20"))

    def test_a_vault_never_checked_needs_five_pages(self):
        self.assertIsNone(self.maintenance_line(today="2026-06-01"))  # 2 indexed pages, no seals
        names = [f"P{n}" for n in range(1, 6)]
        for name in names:
            self.write(f"Pages/{name}.md", "text\n")
        self.index("".join(f"## [[{name}]]\n- Concept {name}\n\n" for name in names))
        self.assertEqual(self.maintenance_line(), "  /maintenance 5 pages not checked yet")
        self.set_config(language="pt")
        self.assertEqual(self.maintenance_line(), "  /maintenance 5 páginas sem verificação")

    def set_records(self, **values):
        path = self.vault / ".srs/state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        for record in state["concepts"].values():
            record.update(values)
        path.write_text(json.dumps(state), encoding="utf-8")

    def test_a_gap_that_wont_close_suggests_it(self):
        self.add("Eigenvectors")
        self.srs("verify", "Eigendecomposition", "Bayes")
        self.set_records(gap={"idea": "an idea", "fails": 2})
        self.assertIsNone(self.maintenance_line())
        self.set_records(gap={"idea": "an idea", "fails": 3})
        self.assertEqual(self.maintenance_line(), "  /maintenance 1 gap that won't close")
        self.set_config(language="pt")
        self.assertEqual(self.maintenance_line(), "  /maintenance 1 lacuna que não fecha")

    def test_a_leech_suggests_it_and_the_parts_join(self):
        names = self.seal_pages(2)
        self.change_pages(names[:1])
        self.index("".join(f"## [[{n}]]\n- Concept {n}\n\n" for n in names) + "## [[Bayes]]\n- Eigenvectors\n- Eigenvalues\n")
        self.add("Eigenvectors", "Eigenvalues")
        self.set_records(lapses=8, gap={"idea": "an idea", "fails": 3})
        self.assertEqual(self.maintenance_line(), "  /maintenance 2 pages changed since the last check (10 days ago); "
                                                  "2 gaps that won't close; 2 leeches")

    def test_a_broken_index_keeps_the_rest_of_the_message(self):
        (self.vault / "index.md").write_bytes(b"\xff\xfe not utf-8")
        self.assertEqual(self.welcome(), "Recall: no reviews scheduled")

    def test_inbox_line_singular_plural_and_newest_first(self):
        self.write("inbox.md", "Intro.\n\n## MLflow\none\n")
        self.assertEqual(self.welcome().splitlines()[1], "  /integrate   1 block in the inbox: MLflow")
        self.write("inbox.md", "## MLflow\none\n\n## URI e URL\ntwo\n")
        self.assertEqual(self.welcome().splitlines()[1], "  /integrate   2 blocks in the inbox: URI e URL, MLflow")
        self.set_config(language="Português")
        self.assertEqual(self.welcome().splitlines()[1], "  /integrate   2 blocos no inbox: URI e URL, MLflow")
        self.write("inbox.md", "## MLflow\none\n")
        self.assertEqual(self.welcome().splitlines()[1], "  /integrate   1 bloco no inbox: MLflow")

    def test_inbox_line_stays_short(self):
        self.write("inbox.md", "".join(f"## B{i}\nx\n" for i in range(1, 6)) + "## " + "L" * 60 + "\nx\n")
        line = self.welcome().splitlines()[1]
        self.assertEqual(line, "  /integrate   6 blocks in the inbox: " + "L" * 39 + "…, B5, B4, +3")

    def test_empty_or_missing_inbox_has_no_integrate_line(self):
        self.write("inbox.md", "Only an intro, no blocks.\n")
        self.assertNotIn("/integrate", self.welcome())
        (self.vault / "inbox.md").unlink()
        self.assertNotIn("/integrate", self.welcome())

    def test_custom_inbox_path_and_heading_level(self):
        self.set_config(paths={"inbox": "Notes/in.md"}, inbox_heading_level=3)
        self.write("Notes/in.md", "## Not a block\n### A block\nx\n")
        self.assertIn("1 block in the inbox: A block", self.welcome())

    def test_interrupted_session_comes_first(self):
        self.add(*self.NAMES)
        self.write("inbox.md", "## MLflow\none\n")
        self.write(".srs/session.json", "not even json")
        self.assertEqual(self.welcome().splitlines(), [
            "Recall: interrupted session! Run a Recall command to recover it.",
            "Recall: today 3 questions: 3 new concepts",
            "  /quiz        take today's quiz",
            "  /integrate   1 block in the inbox: MLflow",
        ])
        self.set_config(language="Português")
        self.assertEqual(self.welcome().splitlines()[0],
                         "Recall: sessão interrompida! Rode um comando do Recall para recuperá-la.")

    def test_outside_a_vault_prints_nothing(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        self.assertSilent(self.run_welcome(stdin=json.dumps({"cwd": str(outside)}), cwd=outside))

    def test_folder_comes_from_stdin_then_project_dir_then_current(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        inner = self.vault / "Math"
        expected = "Recall: no reviews scheduled"
        self.assertEqual(self.welcome(stdin=json.dumps({"cwd": str(inner)}), cwd=outside), expected)
        self.assertSilent(self.run_welcome(stdin=json.dumps({"cwd": str(outside)}),
                                           env_extra={"CLAUDE_PROJECT_DIR": str(self.vault)}, cwd=self.vault))
        for stdin in ("", "not json", "[1]", "{}"):
            self.assertEqual(self.welcome(stdin=stdin, env_extra={"CLAUDE_PROJECT_DIR": str(inner)}, cwd=outside), expected)
            self.assertEqual(self.welcome(stdin=stdin, cwd=inner), expected)
            self.assertSilent(self.run_welcome(stdin=stdin, cwd=outside))

    def test_invalid_files_do_not_raise(self):
        self.add(*self.NAMES)
        good = {rel: (self.vault / rel).read_bytes() for rel in (".srs/state.json", ".srs/config.json", "inbox.md")}
        for rel, bad in ((".srs/state.json", b"{broken"), (".srs/state.json", b"[1, 2]"),
                         (".srs/state.json", b'{"concepts": {"X": {}}}'),
                         (".srs/config.json", b"{broken"), (".srs/config.json", b"[1]"),
                         (".srs/config.json", b'{"scheduling": {"daily_limit": "many"}}'),
                         ("inbox.md", b"## \xff\xfe\n"), (".srs/log.csv", b"a,b\n1,2\n")):
            (self.vault / rel).write_bytes(bad)
            self.assertSilent(self.run_welcome())
            if rel in good:
                (self.vault / rel).write_bytes(good[rel])
            else:
                (self.vault / rel).unlink()
        self.assertSilent(self.run_welcome(env_extra={"RECALL_TODAY": "not a date"}))
        self.welcome()

    def test_writes_nothing_and_never_calls_git(self):
        self.add(*self.NAMES)
        self.write("inbox.md", "## MLflow\none\n")
        self.write(".srs/session.json", "{}")
        files = lambda: {p: (p.stat().st_mtime_ns, p.read_bytes())
                         for p in self.vault.rglob("*") if p.is_file() and ".git" not in p.parts}
        before = (self.git("status", "--porcelain"), self.git("rev-parse", "HEAD"), files())
        self.welcome()
        self.assertEqual((self.git("status", "--porcelain"), self.git("rev-parse", "HEAD"), files()), before)
        # A git that records being called stays uncalled.
        fake = self.tmp / "fake-bin"
        fake.mkdir()
        (fake / "git").write_text("#!/bin/sh\ntouch \"$0.called\"\nexit 1\n", encoding="utf-8")
        (fake / "git").chmod(0o755)
        self.welcome(env_extra={"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"})
        self.assertFalse((fake / "git.called").exists())

    def hook_entry(self):
        return json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]

    def test_hook_entry_runs_only_at_startup_and_keeps_the_guard(self):
        hooks = self.hook_entry()
        self.assertEqual([(e["matcher"], [h["command"] for h in e["hooks"]]) for e in hooks["SessionStart"]],
                         [("startup", ['python3 "${CLAUDE_PLUGIN_ROOT}/scripts/srs.py" welcome'])])
        self.assertEqual([(e["matcher"], [h["command"] for h in e["hooks"]]) for e in hooks["PreToolUse"]],
                         [("Edit|Write|MultiEdit|NotebookEdit|Bash", ['python3 "${CLAUDE_PLUGIN_ROOT}/hooks/guard.py"']),
                          ("SubagentHandback", ['python3 "${CLAUDE_PLUGIN_ROOT}/scripts/srs.py" capture-verdict'])])

    def test_hook_command_with_spaces_and_accents_in_the_paths(self):
        plugin = self.tmp / "Área de Plugins" / "recall"
        plugin.parent.mkdir()
        plugin.symlink_to(ROOT, target_is_directory=True)
        vault = self.tmp / "Área de Trabalho" / "my vault"
        vault.parent.mkdir()
        self.vault.rename(vault)
        self.vault = vault
        (vault / "Math").mkdir(exist_ok=True)
        self.write("inbox.md", "## Fração\nx\n")
        command = self.hook_entry()["SessionStart"][0]["hooks"][0]["command"]
        for env in ({}, {"LC_ALL": "C", "LANG": "C"}):
            for payload in (json.dumps({"cwd": str(vault), "source": "startup"}),
                            json.dumps({"cwd": str(vault), "source": "startup"}, ensure_ascii=False)):
                proc = subprocess.run(["sh", "-c", command], input=payload.encode("utf-8"), cwd=str(self.tmp),
                                      env=dict(self.env, CLAUDE_PLUGIN_ROOT=str(plugin), RECALL_TODAY=self.DAY, **env),
                                      capture_output=True)
                self.assertEqual((proc.returncode, proc.stderr), (0, b""))
                self.assertIn("1 block in the inbox: Fração", json.loads(proc.stdout.decode())["systemMessage"])

    def test_docs_describe_it(self):
        for name in ("docs/usage.md", "docs/design.md"):
            self.assertIn("SessionStart", (ROOT / name).read_text(encoding="utf-8"), name)


class ContextTest(VaultCase):
    def test_contexts_print_without_errors(self):
        self.index("## [[Bayes]]\n- Bayes' theorem\n")
        for command in ("setup", "integrate", "quiz", "promote", "maintenance", "config"):
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

    def test_promote_context_asks_to_audit_candidate_pages_without_a_seal(self):
        self.index("## [[Eigendecomposition]]\n- Eigenvectors\n\n## [[Bayes]]\n- Bayes' theorem\n")
        self.srs("verify", "Math/Bayes.md", "Eigendecomposition")
        out = self.srs("context", "promote").stdout
        self.assertNotIn("## Audit first", out)
        self.write("Math/Bayes.md", "# Bayes\nchanged\n")
        out = self.srs("context", "promote").stdout
        self.assertIn("## Audit first (1 candidate pages without a valid seal)", out)
        self.assertIn("### Math/Bayes.md (audit first)", out)
        self.assertIn("### Math/Eigendecomposition.md\n", out)
        self.assertNotIn("Held back", out)


class StatsTest(VaultCase):
    def log(self, *groups):
        """Write .srs/log.csv from (count, result, phase, interval) groups, all on the study day."""
        lines = ["day,concept,result,phase,interval"]
        for count, result, phase, interval in groups:
            lines += [f"2026-01-10,C{len(lines)},{result},{phase},{interval}" for _ in range(count)]
        (self.vault / ".srs/log.csv").write_text("\n".join(lines) + "\n")

    def test_splits_young_and_mature_and_counts_only_review_answers(self):
        self.log((27, "pass", "review", 5), (3, "fail", "review", 20),
                 (36, "pass", "review", 21), (4, "fail", "review", 90),
                 (50, "fail", "new", 0), (50, "fail", "learning", 0), (50, "fail", "relearning", 30))
        out = self.srs("stats").stdout
        self.assertIn("young (interval under 21 days): 90% of 30 (on target)", out)
        self.assertIn("mature (interval of 21 days or more): 90% of 40 (on target)", out)
        self.assertIn("combined: 90% of 70", out)
        self.assertIn("Calibrate on mature retention.", out)

    def test_a_group_below_30_reviews_has_not_enough_data(self):
        self.log((30, "pass", "review", 3), (5, "pass", "review", 40))
        out = self.srs("stats").stdout
        self.assertIn("young (interval under 21 days): 100% of 30 (above 95%", out)
        self.assertIn("mature (interval of 21 days or more): not enough data (5 of the 30 needed)", out)
        self.assertIn("Calibrate on young retention until mature has 30 reviews.", out)

    def test_threshold_comes_from_config(self):
        config = json.loads((self.vault / ".srs/config.json").read_text())
        config["scheduling"]["mature_interval"] = 7
        (self.vault / ".srs/config.json").write_text(json.dumps(config))
        self.log((30, "pass", "review", 7), (30, "fail", "review", 6))
        out = self.srs("stats").stdout
        self.assertIn("mature (interval of 7 days or more): 100% of 30", out)
        self.assertIn("young (interval under 7 days): 0% of 30 (below 80%", out)

    def test_old_log_gets_the_interval_column_and_its_rows_count_only_in_combined(self):
        self.index("## [[Bayes]]\n- Bayes' theorem\n")
        self.srs("add", "--source", "promote", "Bayes' theorem")
        (self.vault / ".srs/log.csv").write_text("day,concept,result,phase\n2026-01-10,Old,pass,review\n")
        self.srs("record", "Bayes' theorem", "pass", today="2026-01-11")
        lines = (self.vault / ".srs/log.csv").read_text().splitlines()
        self.assertEqual(lines, ["day,concept,result,phase,interval", "2026-01-10,Old,pass,review",
                                 "2026-01-11,Bayes' theorem,pass,new,0"])
        out = self.srs("stats", today="2026-01-11").stdout
        self.assertIn("young (interval under 21 days): not enough data (0 of the 30 needed)", out)
        self.assertIn("combined: 100% of 1", out)
        self.assertIn("1 older review(s) logged without an interval", out)
        self.assertIn("Too few reviews to calibrate yet.", out)


class FlagTest(VaultCase):
    """/quiz and /maintenance flag thin pages and misplaced concepts; /maintenance reports both."""

    def setUp(self):
        super().setUp()
        self.index("## [[Eigendecomposition]]\n- Eigenvectors\n- Matrix norm\n")

    def flags(self):
        return json.loads((self.vault / ".srs/state.json").read_text(encoding="utf-8"))["flags"]

    def test_thin_flag_still_works(self):
        self.srs("flag", "Math/Bayes.md")
        self.assertEqual(self.flags()["thin"], {"Math/Bayes.md": "2026-01-10"})
        self.assertEqual(self.flags()["misplaced"], {})
        self.assertIn("Pages flagged as thin: 1", self.srs("check").stdout)
        self.srs("unflag", "Math/Bayes.md")
        self.assertEqual(self.flags()["thin"], {})

    def test_misplaced_flag_is_stored_reported_and_removed(self):
        out = self.srs("flag", "Math/Eigendecomposition.md", "--misplaced", "matrix norm", "--to", "Linear algebra")
        self.assertIn("Matrix norm", out.stdout)
        self.assertEqual(self.flags()["misplaced"],
                         {"Matrix norm": {"page": "Math/Eigendecomposition.md", "to": "Linear algebra", "day": "2026-01-10"}})
        self.assertEqual(self.flags()["thin"], {})
        report = self.srs("check").stdout
        self.assertIn("Concepts flagged as misplaced: 1", report)
        self.assertIn("Matrix norm: defined on Math/Eigendecomposition.md, belongs in Linear algebra", report)
        self.srs("unflag", "Math/Eigendecomposition.md", "--misplaced", "Matrix norm")
        self.assertEqual(self.flags()["misplaced"], {})
        self.assertIn("Concepts flagged as misplaced: none", self.srs("check").stdout)

    def test_misplaced_flag_without_a_destination(self):
        self.srs("flag", "Math/Eigendecomposition.md", "--misplaced", "Matrix norm")
        self.assertIn("Matrix norm: defined on Math/Eigendecomposition.md (flagged 2026-01-10)", self.srs("check").stdout)

    def test_misplaced_flag_needs_an_indexed_concept_and_a_page(self):
        bad = self.srs("flag", "Math/Eigendecomposition.md", "--misplaced", "Ghost", check=False)
        self.assertEqual(bad.returncode, 2)
        self.assertIn("not in the index", bad.stderr + bad.stdout)
        bad = self.srs("flag", "Math/Nope.md", "--misplaced", "Matrix norm", check=False)
        self.assertEqual(bad.returncode, 2)
        bad = self.srs("flag", "Math/Eigendecomposition.md", "--to", "Somewhere", check=False)
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(self.flags()["misplaced"], {})

    def test_state_without_the_new_flag_type_still_loads(self):
        path = self.vault / ".srs/state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["flags"] = {"thin": {"Math/Bayes.md": "2026-01-01"}}
        path.write_text(json.dumps(state), encoding="utf-8")
        self.assertIn("Concepts flagged as misplaced: none", self.srs("check").stdout)
        self.srs("flag", "Math/Eigendecomposition.md", "--misplaced", "Matrix norm")
        self.assertEqual(sorted(self.flags()["thin"]), ["Math/Bayes.md"])

    def test_unflagging_an_unknown_concept_changes_nothing(self):
        out = self.srs("unflag", "Math/Eigendecomposition.md", "--misplaced", "Matrix norm")
        self.assertIn("was not flagged as misplaced", out.stdout)


class IntegrateOrderTest(unittest.TestCase):
    """The skill, the README, the manual and the design describe the same order of blocks."""

    DOCS = ("skills/integrate/SKILL.md", "README.md", "docs/usage.md", "docs/design.md")

    def test_blocks_follow_the_didactic_order_of_the_plan(self):
        for name in self.DOCS:
            text = (ROOT / name).read_text()
            self.assertIn("didactic order", text, name)
            self.assertIn("from the newest to the oldest", text, name)
            self.assertNotIn("from the oldest to the newest", text, name)
            self.assertNotIn("the order in which", text, name)

    def test_existing_text_is_only_added_to(self):
        skill = (ROOT / "skills/integrate/SKILL.md").read_text()
        self.assertIn("not at the end of the page by default", skill)
        self.assertIn("Never move or rewrite the text already on the page unless an item of the approved plan says to", skill)


class QuizAsksItselfTest(unittest.TestCase):
    """/quiz asks every question itself, from the owner page; no subagent drafts them."""

    def test_no_question_drafter_anywhere(self):
        self.assertFalse((ROOT / "agents/question-drafter.md").exists())
        self.assertEqual(
            sorted(p.name for p in (ROOT / "agents").iterdir()),
            ["concept-judge.md", "diff-judge.md", "index-drafter.md", "page-auditor.md"],
        )
        for path in ("skills", "agents", "hooks", "docs", "README.md", "rules.md", "scripts"):
            target = ROOT / path
            files = [target] if target.is_file() else [
                f for f in target.rglob("*") if f.is_file() and f.suffix in (".md", ".py", ".json")
            ]
            for f in files:
                self.assertNotIn("question-drafter", f.read_text(), str(f))

    def test_quiz_skill_reads_the_page_and_asks_each_question(self):
        skill = (ROOT / "skills/quiz/SKILL.md").read_text()
        self.assertIn("1. Read its page and find what the page says about the concept.", skill)
        self.assertIn("2. Ask one question, following the quiz rules", skill)
        for gone in ("run_in_background", "batch", "drafter", "Silence when", "THIN", "SECOND"):
            self.assertNotIn(gone, skill)

    def test_scheduling_steps_are_unchanged(self):
        skill = (ROOT / "skills/quiz/SKILL.md").read_text()
        self.assertIn('srs.py record "<Concept>" pass', skill)
        self.assertIn('srs.py flag "<page path>"', skill)
        self.assertIn('srs.py commit -m "quiz: <score>"', skill)
        self.assertIn("Don't record these.", skill)

    def test_prerequisite_and_misplaced_steps(self):
        skill = (ROOT / "skills/quiz/SKILL.md").read_text()
        self.assertIn('srs.py gap "<Prerequisite>"', skill)
        self.assertIn('srs.py flag "<page path>" --misplaced "<Concept>"', skill)
        self.assertIn("Only on approval", skill)
        maintenance = (ROOT / "skills/maintenance/SKILL.md").read_text()
        self.assertIn('srs.py unflag "<page>" --misplaced "<Concept>"', maintenance)
        self.assertIn("[[Page#Concept]]", (ROOT / "skills/integrate/SKILL.md").read_text())

    def test_readme_and_design_describe_it(self):
        for name in ("README.md", "docs/usage.md", "docs/design.md"):
            text = (ROOT / name).read_text()
            self.assertNotIn("question-drafter", text, name)
            self.assertNotIn("in the background", text, name)
        self.assertIn("index-drafter and page-auditor (Haiku, read-only)", (ROOT / "README.md").read_text())


class VerdictParserTest(unittest.TestCase):
    ID = "0a1b2c3d"

    def diff_verdict(self, *lines):
        return "\n".join([f"BLOCK {self.ID}", "JUDGE diff-judge", *lines])

    def test_valid_verdicts(self):
        text = self.diff_verdict("VERDICT PASS", "V PASS", "P PASS", "E PASS", "R PASS")
        parsed = srs.parse_verdict(text)
        self.assertEqual((parsed["block"], parsed["judge"], parsed["verdict"]), (self.ID, "diff-judge", "PASS"))
        fenced = "```text\n" + self.diff_verdict("VERDICT REVISE", "V PASS", "P FAIL — a.md: \"x\" — wrong place",
                                                 "P FAIL — b.md: \"y\" — also", "E PASS", "R PASS") + "\n```"
        parsed = srs.parse_verdict(fenced)
        self.assertEqual((parsed["verdict"], parsed["failed"]), ("REVISE", ["P"]))
        concept = f"BLOCK {self.ID}-2\nJUDGE concept-judge\nVERDICT PASS\nO PASS\nD PASS\nI PASS"
        self.assertEqual(srs.parse_verdict(concept)["block"], self.ID + "-2")

    def test_invalid_verdicts(self):
        ok = ["V PASS", "P PASS", "E PASS", "R PASS"]
        bad = {
            "a letter is missing": self.diff_verdict("VERDICT PASS", *ok[:3]),
            "a letter of the other judge": self.diff_verdict("VERDICT PASS", *ok, "O PASS"),
            "a letter twice": self.diff_verdict("VERDICT PASS", *ok, "V PASS"),
            "pass and fail for one letter": self.diff_verdict("VERDICT REVISE", *ok, "V FAIL — a.md: \"x\" — no"),
            "pass verdict with a fail": self.diff_verdict("VERDICT PASS", "V FAIL — a.md: \"x\" — no", *ok[1:]),
            "revise verdict without a fail": self.diff_verdict("VERDICT REVISE", *ok),
            "extra text": self.diff_verdict("VERDICT PASS", *ok) + "\nLooks good to me!",
            "text before": "Here is my verdict:\n" + self.diff_verdict("VERDICT PASS", *ok),
            "wrong verdict word": self.diff_verdict("VERDICT OK", *ok),
            "unknown judge": f"BLOCK {self.ID}\nJUDGE other-judge\nVERDICT PASS\nV PASS\nP PASS\nE PASS\nR PASS",
            "bad block id": "BLOCK ../etc\nJUDGE diff-judge\nVERDICT PASS\n" + "\n".join(ok),
            "a fail without a reason": self.diff_verdict("VERDICT REVISE", "V FAIL", *ok[1:]),
            "empty": "",
        }
        for name, text in bad.items():
            with self.subTest(name):
                with self.assertRaises(srs.VerdictError):
                    srs.parse_verdict(text)


class AgentReviewTest(VaultCase):
    """integrate_review: agent. The commands the skill calls around each block."""

    EIGEN = "# Eigen\nAv = λv defines eigenvectors.\n"

    def setUp(self):
        super().setUp()
        self.index("## [[Eigendecomposition]]\n- Eigenvectors\n\n## [[Bayes]]\n- Bayes' theorem\n")
        self.srs("begin", "integrate")  # also commits the index written above as a manual edit
        self.first = self.block_id("First")
        self.second = self.block_id("Second")

    def block_id(self, title):
        for line in self.srs("inbox", "list").stdout.splitlines():
            m = re.match(r"\s*\[([0-9a-f]{8}(?:-\d+)?)\] (.*) \(\d+ characters\)$", line)
            if m and m.group(2) == title:
                return m.group(1)
        self.fail(f"no block {title}")

    def plan(self, *lines):
        return self.srs("plan", "save", *lines).stdout

    def prepare(self, block_id=None, check=True):
        return self.srs("review", "prepare", block_id or self.first, check=check)

    def hook(self, payload, vault=None):
        proc = subprocess.run([sys.executable, str(SRS), "capture-verdict"], input=json.dumps(payload),
                              cwd=str(vault or self.vault), env=self.env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def verdict_text(self, judge, verdict="PASS", block_id=None, failures=None):
        failures = failures or {}
        letters = srs.JUDGES[judge]
        lines = [f"BLOCK {block_id or self.first}", f"JUDGE {judge}", f"VERDICT {verdict}"]
        for letter in letters:
            lines.append(f"{letter} FAIL — {failures[letter]}" if letter in failures else f"{letter} PASS")
        return "\n".join(lines)

    def judge(self, judge, verdict="PASS", block_id=None, via="stop", agent_id="agent-1", **kw):
        text = self.verdict_text(judge, verdict, block_id, **kw)
        if via == "handback":
            payload = {"hook_event_name": "PreToolUse", "tool_name": "SubagentHandback", "agent_type": f"recall:{judge}",
                       "agent_id": agent_id, "tool_input": {"message": text}, "cwd": str(self.vault)}
        else:
            payload = {"hook_event_name": "SubagentStop", "agent_type": f"recall:{judge}", "agent_id": agent_id,
                       "last_assistant_message": text, "stop_hook_active": False, "cwd": str(self.vault)}
        return self.hook(payload)

    def verdict_line(self, block_id=None):
        return self.srs("review", "verdict", block_id or self.first).stdout.strip().splitlines()[-1]

    def review_files(self, block_id=None):
        return sorted(p.name for p in (self.vault / ".srs/review" / (block_id or self.first)).iterdir())

    def test_plan_save_prints_what_it_saved(self):
        out = self.plan("1. Eigen: First (id)", "2. Bayes: Second (id)")
        self.assertIn("Plan saved (2 lines)", out)
        self.assertEqual((self.vault / ".srs/review/plan.md").read_text(), "1. Eigen: First (id)\n2. Bayes: Second (id)\n")
        self.assertEqual(self.srs("plan", "save", " ", check=False).returncode, 1)

    def test_prepare_needs_a_plan_and_a_block(self):
        proc = self.prepare(check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("No plan saved", proc.stderr)
        self.plan("1. anything")
        self.assertEqual(self.prepare("deadbeef", check=False).returncode, 2)

    def test_prepare_writes_the_judges_input(self):
        self.plan("1. Eigendecomposition: add First after the first paragraph")
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "\none\n")
        out = self.prepare().stdout
        self.assertIn("Checks: ok", out)
        self.assertIn("Judges: recall:diff-judge\n", out)
        path = Path(out.split("Input for recall:diff-judge: ")[1].strip())
        self.assertEqual(path, self.vault / ".srs/review" / self.first / "input-diff-judge.md")
        text = path.read_text()
        for part in ("## Block text", "## First", "one", "## Approved plan", "add First after the first paragraph",
                     "+one", "Math/Eigendecomposition.md"):
            self.assertIn(part, text)

    def test_the_diff_judge_does_not_see_the_index(self):
        self.plan("1. Eigendecomposition: add First at the end")
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "\n## Eigenvalues\none\n")
        self.edit_via_hook("index.md", (self.vault / "index.md").read_text().replace(
            "- Eigenvectors\n", "- Eigenvectors\n- Eigenvalues — The scalars λ in Av = λv\n"))
        out = self.prepare().stdout
        self.assertIn("Judges: recall:diff-judge, recall:concept-judge\n", out)
        folder = self.vault / ".srs/review" / self.first
        self.assertIn(f"Input for recall:diff-judge: {(folder / 'input-diff-judge.md').as_posix()}", out)
        self.assertIn(f"Input for recall:concept-judge: {(folder / 'input-concept-judge.md').as_posix()}", out)
        diff_input = (folder / "input-diff-judge.md").read_text()
        concept_input = (folder / "input-concept-judge.md").read_text()
        for text in (diff_input, concept_input):
            self.assertIn("+## Eigenvalues", text)
        for part in ("The scalars λ", "index.md", "New concepts in the index", "Index lines changed"):
            self.assertNotIn(part, diff_input)
            self.assertIn(part, concept_input)
        self.assertIn(str(self.vault / "index.md"), concept_input)

    def test_edits_outside_edit_and_write_are_flagged(self):
        self.plan("1. Eigendecomposition: add First at the end")
        (self.vault / "Math/Eigendecomposition.md").write_text(self.EIGEN + "\nwritten through the shell\n")
        self.edit_via_hook("index.md", (self.vault / "index.md").read_text() + "- Eigenvalues\n")
        warning = "Warning: changed but not recorded by this session, so left out of the diff: Math/Eigendecomposition.md."
        self.assertIn(warning, self.prepare().stdout)
        self.assertIn(warning, self.srs("diff").stdout)
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "\nwritten through the shell\n")
        self.assertNotIn("Warning: changed but not recorded", self.prepare().stdout)
        self.assertNotIn("Warning: changed but not recorded", self.srs("diff").stdout)

    def test_moved_and_rewritten_lines_are_told_apart(self):
        self.plan("1. move the Bayes line to Eigendecomposition; rewrite Eigendecomposition's first line")
        self.edit_via_hook("Math/Bayes.md", "# Bayes\n")
        self.edit_via_hook("Math/Eigendecomposition.md", "# Eigen\nAv = λv, reworded.\nP(A|B) = P(B|A)P(A)/P(B)\n")
        out = self.prepare().stdout
        self.assertIn("Checks: ok", out)
        text = (self.vault / ".srs/review" / self.first / "input-diff-judge.md").read_text()
        moved = text.split("Moved lines")[1].split("Existing lines rewritten")[0]
        rewritten = text.split("Existing lines rewritten or removed:")[1].split("New headings")[0]
        self.assertIn("Math/Bayes.md: P(A|B) = P(B|A)P(A)/P(B)", moved)
        self.assertNotIn("reworded", moved)
        self.assertIn("Math/Eigendecomposition.md: Av = λv defines eigenvectors.", rewritten)
        self.assertNotIn("P(A|B)", rewritten)

    def test_rewriting_a_page_the_plan_does_not_name_fails(self):
        self.plan("1. Bayes: add First")
        self.edit_via_hook("Math/Eigendecomposition.md", "# Eigen\nreworded\n")
        out = self.prepare().stdout
        self.assertIn("Checks: FAIL", out)
        self.assertIn("Math/Eigendecomposition.md: 1 existing line rewritten or removed", out)
        self.assertNotIn("Judges:", out)
        self.plan("1. Bayes: add First", "2. rewrite Eigendecomposition")
        self.assertIn("Checks: ok", self.prepare().stdout)

    def test_links_must_resolve_and_point_at_real_headings(self):
        self.plan("1. add First to Eigendecomposition")
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "\n[[Nowhere]] [[Bayes#Missing]] [[Bayes#Bayes]] "
                           "[[Bayes|alias]] ![[figure.png]] [[#Eigen]] [[#Nope]]\n")
        out = self.prepare().stdout
        self.assertIn("[[Nowhere]] does not resolve", out)
        self.assertIn("[[Bayes#Missing]] points to a heading that Math/Bayes.md doesn't have", out)
        self.assertIn("[[#Nope]] points to a heading that Math/Eigendecomposition.md doesn't have", out)
        self.assertEqual(out.count("  - "), 3)

    def test_links_to_pages_created_in_the_session_resolve(self):
        self.plan("1. new page Fresh")
        self.edit_via_hook("wiki/Fresh.md", "# Fresh\n## Part\n")
        self.edit_via_hook("Math/Bayes.md", "# Bayes\nP(A|B) = P(B|A)P(A)/P(B)\nSee [[Fresh#Part]].\n")
        self.assertIn("Checks: ok", self.prepare().stdout)

    def test_index_warnings_count_as_failures(self):
        self.plan("1. add First to Eigendecomposition")
        self.edit_via_hook("index.md", "# Index\n\n## [[Eigendecomposition]]\n- Eigenvectors\n- Bayes' theorem\n\n"
                           "## [[Bayes]]\n- Bayes' theorem\n")
        out = self.prepare().stdout
        self.assertIn("Checks: FAIL", out)
        self.assertIn("Bayes' theorem", out)

    def test_the_concept_judge_runs_only_for_new_headings_or_concepts(self):
        self.plan("1. Eigendecomposition: First")
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "plain addition\n")
        self.assertIn("Judges: recall:diff-judge\n", self.prepare().stdout)
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "plain addition\n\n## Trace\nsum of eigenvalues\n")
        self.assertIn("Judges: recall:diff-judge, recall:concept-judge", self.prepare().stdout)
        self.srs("discard")
        self.edit_via_hook("index.md", "# Index\n\n## [[Eigendecomposition]]\n- Eigenvectors\n- Trace\n\n"
                           "## [[Bayes]]\n- Bayes' theorem\n")
        self.assertIn("Judges: recall:diff-judge, recall:concept-judge", self.prepare().stdout)
        # a changed description is not a new concept
        self.srs("discard")
        self.edit_via_hook("index.md", "# Index\n\n## [[Eigendecomposition]]\n- Eigenvectors — vectors\n\n"
                           "## [[Bayes]]\n- Bayes' theorem\n")
        self.assertIn("Judges: recall:diff-judge\n", self.prepare().stdout)

    def test_a_moved_heading_is_not_a_new_heading(self):
        self.write("Math/Eigendecomposition.md", "# Eigen\n## Trace\nsum\n")
        self.srs("end")
        self.srs("begin", "integrate")
        self.plan("1. move Trace from Eigendecomposition to Bayes")
        self.edit_via_hook("Math/Eigendecomposition.md", "# Eigen\n")
        self.edit_via_hook("Math/Bayes.md", "# Bayes\nP(A|B) = P(B|A)P(A)/P(B)\n## Trace\nsum\n")
        self.assertIn("Judges: recall:diff-judge\n", self.prepare().stdout)

    def test_the_hook_saves_a_verdict_from_the_handback_or_the_final_message(self):
        self.plan("1. First")
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "one\n")
        self.prepare()
        self.assertEqual(self.verdict_line(), "Review: MISSING diff-judge")
        self.assertEqual(self.judge("diff-judge", via="handback"), "")
        saved = json.loads((self.vault / ".srs/review" / self.first / "diff-judge.json").read_text())
        self.assertEqual(saved["agent_id"], "agent-1")
        self.assertEqual(self.verdict_line(), "Review: PASS")
        self.assertEqual(self.judge("diff-judge", via="stop", agent_id="agent-2", verdict="REVISE",
                                    failures={"V": 'Math/Eigendecomposition.md: "one" — padding'}), "")
        self.assertEqual(self.verdict_line(), "Review: REVISE")
        self.assertIn('V FAIL — Math/Eigendecomposition.md: "one" — padding', self.srs("review", "verdict", self.first).stdout)

    def test_the_stop_event_after_a_handback_changes_nothing(self):
        self.plan("1. First")
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "one\n")
        self.prepare()
        self.judge("diff-judge", via="handback", agent_id="agent-9")
        payload = {"hook_event_name": "SubagentStop", "agent_type": "recall:diff-judge", "agent_id": "agent-9",
                   "last_assistant_message": "Done, the verdict is above.", "stop_hook_active": False, "cwd": str(self.vault)}
        self.assertEqual(self.hook(payload), "")
        self.assertEqual(self.verdict_line(), "Review: PASS")

    def test_an_invalid_verdict_is_sent_back_to_the_judge(self):
        self.plan("1. First")
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "one\n")
        self.prepare()
        bad = {"hook_event_name": "PreToolUse", "tool_name": "SubagentHandback", "agent_type": "recall:diff-judge",
               "agent_id": "a1", "tool_input": {"message": "All good!"}, "cwd": str(self.vault)}
        out = json.loads(self.hook(bad))["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("Invalid verdict", out["permissionDecisionReason"])
        stop = {"hook_event_name": "SubagentStop", "agent_type": "recall:diff-judge", "agent_id": "a1",
                "last_assistant_message": "All good!", "stop_hook_active": False, "cwd": str(self.vault)}
        out = json.loads(self.hook(stop))
        self.assertEqual(out["decision"], "block")
        self.assertIn("exactly the format", out["reason"])
        self.assertEqual(self.hook(dict(stop, stop_hook_active=True)), "")  # asked once already: no loop
        self.assertEqual(self.verdict_line(), "Review: MISSING diff-judge")

    def test_the_hook_ignores_what_is_not_a_judge_report_for_this_block(self):
        self.plan("1. First")
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "one\n")
        self.prepare()
        text = self.verdict_text("diff-judge")
        base = {"hook_event_name": "SubagentStop", "agent_id": "a1", "last_assistant_message": text,
                "stop_hook_active": False, "cwd": str(self.vault)}
        self.assertEqual(self.hook(dict(base, agent_type="recall:page-auditor")), "")
        self.assertEqual(self.hook(dict(base, agent_type="Explore")), "")
        self.assertEqual(self.hook(dict(base, agent_type="")), "")
        self.assertNotEqual(self.hook(dict(base, agent_type="recall:concept-judge")), "")  # wrong JUDGE line: sent back
        other = self.verdict_text("diff-judge", block_id="deadbeef")
        self.assertNotEqual(self.hook(dict(base, agent_type="recall:diff-judge", last_assistant_message=other)), "")
        concept = self.verdict_text("concept-judge")  # not requested for this block
        self.assertNotEqual(self.hook(dict(base, agent_type="recall:concept-judge", last_assistant_message=concept)), "")
        self.assertEqual(self.verdict_line(), "Review: MISSING diff-judge")
        self.assertEqual(self.hook(dict(base, agent_type="recall:diff-judge", hook_event_name="SubagentStart")), "")
        self.srs("discard")
        self.srs("end")
        self.assertEqual(self.hook(dict(base, agent_type="recall:diff-judge")), "")  # no session: nothing to do

    def test_the_hook_log_is_opt_in(self):
        self.plan("1. First")
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "one\n")
        self.prepare()
        self.judge("diff-judge")
        self.assertFalse((self.vault / ".srs/review/hook-log.jsonl").exists())
        self.env["RECALL_HOOK_LOG"] = "1"
        self.judge("diff-judge", agent_id="agent-2")
        lines = (self.vault / ".srs/review/hook-log.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(lines[0])["agent_id"], "agent-2")

    def clear_block(self, block_id=None, judges=("diff-judge",)):
        for judge in judges:
            self.judge(judge, block_id=block_id, agent_id=f"agent-{judge}")

    def test_a_cleared_block_is_committed_with_its_verdicts(self):
        self.plan("1. Eigendecomposition: First")
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "one\n")
        self.prepare()
        self.clear_block()
        self.assertEqual(self.verdict_line(), "Review: PASS")
        self.srs("inbox", "remove", self.first)
        out = self.srs("commit", "--review", self.first, "-m", "integrate: First").stdout
        self.assertIn("Committed", out)
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), "integrate: First")
        body = self.git("log", "-1", "--format=%b").strip()
        self.assertEqual(body, "Reviewed by agents.\ndiff-judge: PASS")
        self.assertFalse((self.vault / ".srs/review" / self.first).exists())
        self.assertEqual(sorted(self.git("show", "--name-only", "--format=", "HEAD").split()),
                         ["Math/Eigendecomposition.md", "inbox.md"])
        self.assertNotIn(".srs/review", self.git("status", "--porcelain"))

    def test_a_block_is_not_committed_without_a_clean_review(self):
        self.plan("1. Eigendecomposition: First")
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "one\n")
        proc = self.srs("commit", "--review", self.first, "-m", "integrate: First", check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no review in progress", proc.stderr)
        self.prepare()
        proc = self.srs("commit", "--review", self.first, "-m", "integrate: First", check=False)
        self.assertIn("verdict", proc.stderr)
        self.judge("diff-judge", verdict="REVISE", failures={"V": 'a.md: "x" — no'})
        proc = self.srs("commit", "--review", self.first, "-m", "integrate: First", check=False)
        self.assertIn("review final", proc.stderr)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), "vault: manual edits")

    def test_failed_checks_cannot_be_committed(self):
        self.plan("1. Bayes: First")
        self.edit_via_hook("Math/Eigendecomposition.md", "# Eigen\nreworded\n")
        self.assertIn("Checks: FAIL", self.prepare().stdout)
        proc = self.srs("commit", "--review", self.first, "-m", "integrate: First", check=False)
        self.assertIn("checks", proc.stderr)
        self.assertEqual(proc.returncode, 2)

    def test_editing_after_the_judges_passed_is_refused(self):
        self.plan("1. Eigendecomposition: First")
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "one\n")
        self.prepare()
        self.clear_block()
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "one\nsomething the judge never saw\n")
        proc = self.srs("commit", "--review", self.first, "-m", "integrate: First", check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("changed after the last review step", proc.stderr)
        final = self.srs("review", "final", self.first, "--note", "tweak", check=False)
        self.assertIn("nothing to revise", final.stderr)
        self.prepare()  # starting over has the judges see it
        self.assertEqual(self.verdict_line(), "Review: MISSING diff-judge")

    def test_one_revision_is_allowed_after_a_revise_verdict(self):
        self.plan("1. Eigendecomposition: First")
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "one\npadding\n")
        self.prepare()
        self.judge("diff-judge", verdict="REVISE", failures={"V": 'Math/Eigendecomposition.md: "padding" — not the user\'s'})
        self.assertEqual(self.srs("review", "final", self.first, check=False).returncode, 1)  # --note is required
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "one\n")
        out = self.srs("review", "final", self.first, "--note", "removed the padding").stdout
        self.assertIn("Checks: ok", out)
        self.assertNotIn("padding", (self.vault / ".srs/review" / self.first / "input-diff-judge.md").read_text().split("## Diff")[1])
        again = self.srs("review", "final", self.first, "--note", "again", check=False)
        self.assertIn("already revised once", again.stderr)
        self.srs("inbox", "remove", self.first)
        self.srs("commit", "--review", self.first, "-m", "integrate: First")
        body = self.git("log", "-1", "--format=%b").strip()
        self.assertEqual(body, "Reviewed by agents.\ndiff-judge: REVISE (V) — writer: removed the padding")

    def test_a_revision_that_needs_the_concept_judge_is_not_accepted(self):
        self.plan("1. Eigendecomposition: First")
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "one\npadding\n")
        self.prepare()
        self.judge("diff-judge", verdict="REVISE", failures={"V": 'x.md: "padding" — no'})
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "one\n## Trace\nsum\n")
        out = self.srs("review", "final", self.first, "--note", "added a section").stdout
        self.assertIn("Checks: FAIL", out)
        self.assertIn("now needs concept-judge", out)
        self.assertEqual(self.srs("commit", "--review", self.first, "-m", "integrate: First", check=False).returncode, 2)

    def test_both_judges_are_committed_when_a_concept_is_new(self):
        self.plan("1. Eigendecomposition: First")
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "## Trace\nsum\n")
        self.edit_via_hook("index.md", "# Index\n\n## [[Eigendecomposition]]\n- Eigenvectors\n- Trace\n\n"
                           "## [[Bayes]]\n- Bayes' theorem\n")
        self.assertIn("recall:concept-judge", self.prepare().stdout)
        self.clear_block()
        self.assertEqual(self.verdict_line(), "Review: MISSING concept-judge")
        self.clear_block(judges=("concept-judge",))
        self.srs("inbox", "remove", self.first)
        self.srs("commit", "--review", self.first, "-m", "integrate: First")
        self.assertEqual(self.git("log", "-1", "--format=%b").strip(),
                         "Reviewed by agents.\ndiff-judge: PASS\nconcept-judge: PASS")

    def test_parking_saves_the_proposal_and_leaves_the_vault_clean(self):
        self.plan("1. Eigendecomposition: First; new page Fresh")
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "one\n")
        self.edit_via_hook("wiki/Fresh.md", "# Fresh\nnew\n")
        self.prepare()
        out = self.srs("review", "park", self.first, "--reason", "the judge disagrees").stdout
        self.assertIn("Parked block", out)
        self.assertEqual((self.vault / "Math/Eigendecomposition.md").read_text(), self.EIGEN)
        self.assertFalse((self.vault / "wiki/Fresh.md").exists())
        self.assertEqual(self.git("status", "--porcelain").strip(), "")
        entry = json.loads((self.vault / ".srs/review/supervision.json").read_text())[self.first]
        self.assertEqual((entry["title"], entry["reason"], entry["patch"]), ("First", "the judge disagrees", True))
        self.assertEqual(sorted(entry["files"]), ["Math/Eigendecomposition.md", "wiki/Fresh.md"])
        self.assertEqual(self.srs("review", "park", self.first, check=False).returncode, 1)  # --reason is required

    def test_a_parked_proposal_can_be_restored_for_the_human_flow(self):
        self.plan("1. Eigendecomposition: First; new page Fresh")
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "one\n")
        self.edit_via_hook("wiki/Fresh.md", "# Fresh\nnew\n")
        self.prepare()
        self.srs("review", "park", self.first, "--reason", "needs a look")
        self.srs("review", "restore", self.first)
        self.assertEqual((self.vault / "wiki/Fresh.md").read_text(), "# Fresh\nnew\n")
        self.assertIn("one", (self.vault / "Math/Eigendecomposition.md").read_text())
        diff = self.srs("diff").stdout
        self.assertIn("wiki/Fresh.md", diff)
        self.assertIn("+one", diff)
        self.srs("commit", "-m", "integrate: First")  # the human flow commits what was restored
        self.assertEqual(sorted(self.git("show", "--name-only", "--format=", "HEAD").split()),
                         ["Math/Eigendecomposition.md", "wiki/Fresh.md"])

    def test_restoring_fails_when_the_pages_moved_on_or_there_is_no_patch(self):
        self.plan("1. Eigendecomposition: First")
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "one\n")
        self.prepare()
        self.srs("review", "park", self.first, "--reason", "r")
        self.edit_via_hook("Math/Eigendecomposition.md", "# Eigen\nsomething else entirely\n")
        self.srs("commit", "-m", "integrate: another block")
        proc = self.srs("review", "restore", self.first, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Patch no longer applies: redo the block.", proc.stderr)
        self.assertEqual(self.srs("review", "restore", "deadbeef", check=False).returncode, 2)
        self.srs("review", "park", self.second, "--waiting", "--reason", "waits for First")
        proc = self.srs("review", "restore", self.second, check=False)
        self.assertIn("No saved proposal", proc.stderr)

    def test_a_block_waiting_for_another_leaves_the_vault_alone(self):
        self.edit_via_hook("Math/Bayes.md", "# Bayes\nunrelated pending edit\n")
        self.srs("review", "park", self.second, "--waiting", "--reason", "waits for First")
        self.assertIn("unrelated pending edit", (self.vault / "Math/Bayes.md").read_text())
        entry = json.loads((self.vault / ".srs/review/supervision.json").read_text())[self.second]
        self.assertEqual((entry["waiting"], entry["patch"], entry["files"]), (True, False, []))

    def test_parked_blocks_show_in_the_context_and_survive_the_session(self):
        self.plan("1. First")
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "one\n")
        self.prepare()
        self.srs("review", "park", self.first, "--reason", "no verdict captured")
        self.srs("end")
        self.assertFalse((self.vault / ".srs/review/plan.md").exists())
        self.assertFalse((self.vault / ".srs/review" / self.first).exists())
        context = self.srs("context", "integrate").stdout
        self.assertIn("## Blocks waiting for supervision", context)
        self.assertIn(f"[{self.first}] First — no verdict captured", context)
        self.srs("begin", "integrate")
        self.assertTrue((self.vault / ".srs/review/supervision.json").exists())
        self.srs("inbox", "remove", self.first)  # integrated by hand: no longer waiting
        self.assertFalse((self.vault / ".srs/review/supervision.json").exists())
        self.assertNotIn("waiting for supervision", self.srs("context", "integrate").stdout)

    def test_an_edited_block_is_no_longer_waiting(self):
        self.srs("review", "park", self.second, "--waiting", "--reason", "r")
        self.srs("end")
        self.write("inbox.md", "Intro.\n\n## First\none\n\n## Second\ntwo, edited\n")
        self.assertNotIn("waiting for supervision", self.srs("context", "integrate").stdout)
        self.srs("begin", "integrate")
        self.assertFalse((self.vault / ".srs/review/supervision.json").exists())

    def test_review_files_stay_out_of_git_even_in_an_older_vault(self):
        gitignore = self.vault / ".gitignore"
        gitignore.write_text("\n".join(l for l in gitignore.read_text().splitlines() if "review" not in l) + "\n")
        self.srs("end")
        self.srs("begin", "integrate")  # commits the .gitignore change as a manual edit
        self.plan("1. First")
        self.edit_via_hook("Math/Eigendecomposition.md", self.EIGEN + "one\n")
        self.prepare()
        self.srs("review", "park", self.first, "--reason", "r")
        self.assertEqual(self.git("status", "--porcelain").strip(), "")
        self.srs("end")
        self.srs("begin", "quiz")
        self.assertNotIn("review", self.git("ls-files"))
        self.srs("end")

    def test_review_works_in_a_vault_inside_a_bigger_repository(self):
        outer = self.tmp / "outer"
        inner = outer / "notes" / "vault"
        inner.mkdir(parents=True)
        (outer / "README").write_text("outer\n")
        subprocess.run(["git", "init", "-q"], cwd=str(outer), env=self.env, check=True)
        (inner / "a.md").write_text("# A\nold\n")
        (inner / "inbox.md").write_text("## Blk\ntext\n")
        self.srs("init", cwd=inner)
        self.srs("begin", "integrate", cwd=inner)
        block = re.search(r"\[([0-9a-f]{8})\]", self.srs("inbox", "list", cwd=inner).stdout).group(1)
        self.srs("plan", "save", "1. a: Blk", cwd=inner)
        for rel, text in (("a.md", "# A\nold\nnew\n"), ("b.md", "# B\n")):
            self.guard({"tool_name": "Write", "tool_input": {"file_path": str(inner / rel)}, "cwd": str(inner)})
            (inner / rel).write_text(text)
        self.assertIn("Checks: ok", self.srs("review", "prepare", block, cwd=inner).stdout)
        self.srs("review", "park", block, "--reason", "r", cwd=inner)
        self.assertEqual((inner / "a.md").read_text(), "# A\nold\n")
        self.assertFalse((inner / "b.md").exists())
        self.srs("review", "restore", block, cwd=inner)
        self.assertEqual((inner / "a.md").read_text(), "# A\nold\nnew\n")
        self.assertEqual((inner / "b.md").read_text(), "# B\n")
        self.srs("commit", "-m", "integrate: Blk", cwd=inner)
        files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=str(outer), env=self.env,
                               capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(files), ["notes/vault/a.md", "notes/vault/b.md"])
        self.srs("end", cwd=inner)

    def test_new_vaults_ignore_the_review_folder(self):
        self.assertIn(".srs/review/", (self.vault / ".gitignore").read_text())

    def test_config_switches_the_review_mode(self):
        self.srs("end")
        self.assertIn("Review mode: human", self.srs("context", "integrate").stdout)
        out = self.srs("config", "--integrate-review", "agent").stdout
        self.assertIn("config: integrate_review human -> agent", out)
        self.assertIn("/integrate review: agent", out)
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), "config: integrate_review human -> agent")
        self.assertEqual(json.loads((self.vault / ".srs/config.json").read_text())["integrate_review"], "agent")
        self.assertIn("Review mode: agent", self.srs("context", "integrate").stdout)
        self.assertIn("/integrate review: agent", self.srs("context", "config").stdout)
        self.assertIn("Nothing changed.", self.srs("config", "--integrate-review", "agent").stdout)
        self.assertEqual(self.srs("config", "--integrate-review", "robot", check=False).returncode, 2)
        out = self.srs("config", "--integrate-review", "human", "--new-per-day", "3").stdout
        self.assertIn("new_per_day 5 -> 3, integrate_review agent -> human", out)

    def test_an_unknown_review_mode_in_the_config_is_reported(self):
        config = json.loads((self.vault / ".srs/config.json").read_text())
        config["integrate_review"] = "robot"
        (self.vault / ".srs/config.json").write_text(json.dumps(config))
        self.assertIn("ERROR: Unknown integrate_review 'robot'", self.srs("context", "integrate").stdout)

    def test_the_welcome_counts_blocks_waiting_for_supervision(self):
        self.srs("review", "park", self.second, "--waiting", "--reason", "r")
        self.srs("end")
        self.set_language("English")
        self.assertIn("(1 waiting for supervision)", self.run_welcome())
        self.set_language("Brazilian Portuguese")
        self.assertIn("(1 aguardando supervisão)", self.run_welcome())

    def set_language(self, language):
        path = self.vault / ".srs/config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["language"] = language
        path.write_text(json.dumps(config), encoding="utf-8")

    def run_welcome(self):
        proc = subprocess.run([sys.executable, str(SRS), "--today", "2026-01-10", "welcome"], cwd=str(self.vault),
                              env=self.env, capture_output=True, text=True, input="")
        return json.loads(proc.stdout)["systemMessage"]


class ReviewPromptTest(VaultCase):
    """The one-time question about the agent review, and the reminders to try it. Nothing random."""

    def setUp(self):
        super().setUp()
        self.count = 0

    def context(self, today="2026-01-10"):
        return self.srs("context", "integrate", today=today).stdout

    def integrate_commits(self, n, subject="integrate: Block"):
        for _ in range(n):
            self.count += 1
            self.write("Math/Bayes.md", f"# Bayes\nversion {self.count}\n")
            self.git("add", "-A")
            self.git("commit", "-q", "-m", f"{subject} {self.count}")

    def prompt(self):
        return json.loads((self.vault / ".srs/config.json").read_text())["integrate_review_prompt"]

    def test_the_first_use_asks_and_the_answer_is_kept(self):
        self.assertIn("## FIRST USE", self.context())
        self.assertIn("Review mode: human", self.context())
        out = self.srs("config", "--integrate-review", "human").stdout
        self.assertIn("config: integrate_review human kept", out)
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), "config: integrate_review human kept")
        prompt = self.prompt()
        self.assertEqual((prompt["asked"], prompt["wait_days"]), ("2026-01-10", 14))
        self.assertEqual(prompt["asked_head"], self.git("rev-parse", "HEAD~1").strip())
        self.assertNotIn("FIRST USE", self.context())
        self.assertIn("Nothing changed.", self.srs("config", "--integrate-review", "human").stdout)

    def test_choosing_the_agent_review_ends_the_question_too(self):
        self.srs("config", "--integrate-review", "agent")
        self.assertEqual(self.prompt()["asked"], "2026-01-10")
        self.assertNotIn("FIRST USE", self.context())
        self.integrate_commits(12)
        self.assertNotIn("Reminder due", self.context("2026-06-01"))  # nothing to remind in the automatic mode

    def test_an_agent_vault_that_was_never_asked_is_not_asked(self):
        config = json.loads((self.vault / ".srs/config.json").read_text())
        config["integrate_review"] = "agent"
        (self.vault / ".srs/config.json").write_text(json.dumps(config))
        self.assertNotIn("FIRST USE", self.context())

    def test_the_reminder_needs_both_the_days_and_the_commits(self):
        self.srs("config", "--integrate-review", "human")
        self.integrate_commits(9)
        self.assertNotIn("Reminder due", self.context("2026-01-24"))  # 14 days, 9 blocks
        self.integrate_commits(1)
        self.assertNotIn("Reminder due", self.context("2026-01-23"))  # 13 days, 10 blocks
        self.assertIn("## Reminder due", self.context("2026-01-24"))  # 14 days, 10 blocks
        self.assertIn("approved 10 blocks by hand since 2026-01-10", self.context("2026-02-20"))

    def test_only_blocks_count_not_the_review_list_commits(self):
        self.srs("config", "--integrate-review", "human")
        self.integrate_commits(9)
        self.integrate_commits(5, subject="integrate: queue 2 concepts")
        self.integrate_commits(3, subject="quiz: 5/5")
        self.assertNotIn("Reminder due", self.context("2026-03-01"))

    def test_not_now_postpones_the_next_reminder_up_to_the_limit(self):
        self.srs("config", "--integrate-review", "human")
        waits = []
        day = dt.date(2026, 1, 10)
        for _ in range(4):
            self.integrate_commits(10)
            day += dt.timedelta(days=self.prompt()["wait_days"])
            self.assertIn("Reminder due", self.context(day.isoformat()))
            out = self.srs("config", "--remind-later", today=day.isoformat()).stdout
            waits.append(self.prompt()["wait_days"])
            self.assertIn("integrate_review reminder in", out)
        self.assertEqual(waits, [28, 41, 41, 41])
        self.assertEqual(self.prompt()["asked"], day.isoformat())
        self.assertNotIn("Reminder due", self.context(day.isoformat()))
        self.integrate_commits(10)
        self.assertNotIn("Reminder due", self.context((day + dt.timedelta(days=40)).isoformat()))
        self.assertIn("Reminder due", self.context((day + dt.timedelta(days=41)).isoformat()))

    def test_remind_later_only_applies_to_the_manual_mode(self):
        self.srs("config", "--integrate-review", "agent")
        proc = self.srs("config", "--remind-later", check=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("only applies while", proc.stderr)

    def test_switching_back_to_manual_starts_the_wait_again(self):
        self.srs("config", "--integrate-review", "agent")
        self.integrate_commits(12)
        self.srs("config", "--integrate-review", "human", today="2026-03-01")
        self.assertEqual(self.prompt()["asked"], "2026-03-01")
        self.assertNotIn("Reminder due", self.context("2026-03-20"))
        self.integrate_commits(10)
        self.assertIn("Reminder due", self.context("2026-03-15"))

    def test_the_limits_come_from_the_config(self):
        config = json.loads((self.vault / ".srs/config.json").read_text())
        config["integrate_review_prompt"].update(min_commits=2, first_days=3, max_days=5)
        (self.vault / ".srs/config.json").write_text(json.dumps(config))
        self.srs("config", "--integrate-review", "human")
        self.integrate_commits(2)
        self.assertIn("Reminder due", self.context("2026-01-13"))
        self.srs("config", "--remind-later", today="2026-01-13")
        self.assertEqual(self.prompt()["wait_days"], 5)  # 3 doubled, capped at 5

    def test_a_rewritten_history_gives_no_reminder(self):
        self.srs("config", "--integrate-review", "human")
        self.integrate_commits(10)
        config = json.loads((self.vault / ".srs/config.json").read_text())
        config["integrate_review_prompt"]["asked_head"] = "0" * 40
        (self.vault / ".srs/config.json").write_text(json.dumps(config))
        self.assertNotIn("Reminder due", self.context("2026-06-01"))

    def test_the_skill_tells_what_to_do_with_both_messages(self):
        skill = (ROOT / "skills/integrate/SKILL.md").read_text()
        self.assertIn("FIRST USE", skill)
        self.assertIn("Reminder due", skill)
        self.assertIn("srs.py config --remind-later", skill)


class QuizOffDocsTest(unittest.TestCase):
    """The commands follow the quiz setting, and the page auditor notes what /quiz used to find alone."""

    def test_skills_follow_the_quiz_setting(self):
        for name in ("quiz", "promote"):
            self.assertIn("If the block says QUIZ OFF, do what it says, and stop.",
                          (ROOT / f"skills/{name}/SKILL.md").read_text())
        self.assertIn("With the quiz off, skip steps 4 and 5", (ROOT / "skills/integrate/SKILL.md").read_text())
        setup = (ROOT / "skills/setup/SKILL.md").read_text()
        self.assertIn("--quiz <on|off>", setup)
        self.assertIn("`--algorithm` only with the quiz on", setup)
        self.assertIn("srs.py config --quiz on", (ROOT / "skills/config/SKILL.md").read_text())

    def test_the_page_auditor_notes_thin_pages_and_misplaced_concepts(self):
        text = (ROOT / "agents/page-auditor.md").read_text()
        front = text.split("---")[1]
        self.assertIn("model: haiku", front)
        self.assertIn("tools: Read, Glob, Grep\n", front)
        self.assertIn("NOTE <path> — T:", text)
        self.assertIn("NOTE <path> — M:", text)
        self.assertIn("a page with notes still passes", text)
        maintenance = (ROOT / "skills/maintenance/SKILL.md").read_text()
        self.assertIn('for each T note, run `srs.py flag "<path>"`', maintenance)
        self.assertIn("an M note is flagged only on the user's approval", maintenance)
        self.assertIn("run `srs.py flag \"<path>\"` for each T note", (ROOT / "skills/promote/SKILL.md").read_text())


class ReviewAgentsTest(unittest.TestCase):
    """The hook and the two judge agents that the agent review relies on."""

    def agent(self, name):
        text = (ROOT / "agents" / f"{name}.md").read_text()
        return text.split("---")[1], text

    def test_the_hooks_are_registered(self):
        hooks = json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"]
        handback = [h for h in hooks["PreToolUse"] if h["matcher"] == "SubagentHandback"]
        stop = hooks["SubagentStop"]
        self.assertEqual((len(handback), len(stop)), (1, 1))
        for entry in (handback[0], stop[0]):
            self.assertEqual(entry["hooks"][0]["command"],
                             'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/srs.py" capture-verdict')
        matcher = stop[0]["matcher"]
        self.assertTrue(re.fullmatch(matcher, "recall:diff-judge"))
        self.assertTrue(re.fullmatch(matcher, "recall:concept-judge"))
        self.assertFalse(re.search(matcher, "recall:page-auditor"))
        self.assertFalse(re.search(matcher, "diff-judge"))  # a bare name is not the plugin's agent
        guard = [h for h in hooks["PreToolUse"] if h["matcher"] != "SubagentHandback"]
        self.assertIn("guard.py", guard[0]["hooks"][0]["command"])

    def test_the_judges_only_read(self):
        for name, model in (("diff-judge", "haiku"), ("concept-judge", "sonnet")):
            front, text = self.agent(name)
            self.assertIn(f"name: {name}", front)
            self.assertIn(f"model: {model}", front)
            self.assertIn("tools: Read, Glob, Grep\n", front)
            for letter in srs.JUDGES[name]:
                self.assertIn(f"- **{letter}.", text)
            self.assertIn("never edit files", text)

    def test_the_example_in_each_prompt_is_a_valid_verdict(self):
        for name in srs.JUDGES:
            _, text = self.agent(name)
            example = text.split("```text\n")[1].split("```")[0].replace("<id>", "0a1b2c3d")
            parsed = srs.parse_verdict(example)
            self.assertEqual((parsed["judge"], parsed["verdict"]), (name, "REVISE"))
            self.assertEqual(sorted(parsed["results"]), sorted(srs.JUDGES[name]))

    def test_the_judge_names_match_the_hook_and_the_plugin(self):
        manifest = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        self.assertEqual(manifest["name"], srs.JUDGE_PLUGIN)
        for name in srs.JUDGES:
            self.assertTrue((ROOT / "agents" / f"{name}.md").is_file())


if __name__ == "__main__":
    unittest.main()
