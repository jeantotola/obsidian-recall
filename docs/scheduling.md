# Scheduling

This document gives the exact rules of Recall's scheduling algorithms. The [README](../README.md#scheduling) has the summary; the code is in [`scripts/srs.py`](../scripts/srs.py), under "Scheduling".

Answers are pass or fail. The script applies every answer and computes every date; the model never does. Two algorithms are built in:

- **`anki`**, the default: Anki's classic scheduler (SM-2) with Anki's default deck options, adapted to pass and fail.
- **`thirds`**: Recall's own rules, with no ease and a lapse that keeps a third of the interval.

`scheduling.algorithm` in `.srs/config.json` selects one, and `scheduling.anki` and `scheduling.thirds` hold their parameters. Changes apply from the next answer; dates already scheduled stay as they are.

## Rules both algorithms share

**States.** A concept in the schedule is in one of four states:

| State | Meaning |
| --- | --- |
| new | Queued, never asked |
| learning | Failed before its first pass |
| review | Graduated: it has an interval, the days until the next question |
| relearning | Failed after graduating; back the next day |

Each concept stores one date, the study day from which it is due, and stays due until it is asked.

**Study day.** The day starts at 4 a.m. by default (`scheduling.day_starts_at`), so a session after midnight counts as the previous day. A newly queued concept becomes due on the next study day.

**One answer a day.** The script records one answer per concept per study day. `/quiz` asks failed concepts again at the end of the session, but those repeats are practice and aren't recorded.

**Graduation.** The first pass of a new or learning concept moves it to review; with the default parameters it comes back the next day. A fail sends it to learning, back the next day.

**Late reviews.** When a concept is answered after its due date, a pass counts half the delay. Both algorithms reach the same base: `interval + days_late / 2`. A 10-day interval answered 10 days late grows from 15 days instead of 10.

**New content (hold).** When `/integrate` adds content to a concept in review, the concept is put on hold: due the next study day, with a focus line that aims the question at the new part. On that answer, a pass keeps the interval as it was, without growth, and a fail is a normal lapse. A concept in learning or relearning only gets the focus.

**Leeches.** A concept with as many lapses as the algorithm's `leech_threshold` is reported by `/quiz` and `/maintenance`. It is probably too broad (split it) or its page is unclear (rewrite it).

**Determinism.** Fuzz, the small random spread that keeps concepts learned together from staying due on the same days, comes from a hash of the concept name and the day. The same answers always give the same dates, which keeps the state reproducible and the git history meaningful.

## anki

Anki's classic scheduler, the one Anki uses unless you turn on FSRS, with Anki's default deck options. Pass is Anki's Good and fail is Again; Hard and Easy don't exist here.

| State | Pass | Fail |
| --- | --- | --- |
| new, learning | review; interval = `graduating_interval`; ease = `starting_ease` | learning, back the next day |
| review | interval grows (formula below) | a lapse: ease − `ease_penalty`, never below `minimum_ease`; relearning, back the next day |
| relearning | review; interval = the interval set at the lapse | back the next day; nothing else changes |

**Pass in review.** With `I` the current interval, `late` the days past the due date, `E` the concept's ease and `M` the `interval_modifier`:

```text
hard  = max(round(I × hard_interval × M), I + 1)
good  = (I + late / 2) × E × M
new   = max(good, hard + 1), capped at maximum_interval, then fuzzed
```

The Hard interval is never offered, but as in Anki it sets the minimum growth of a pass: at least two days more than the current interval. With `hard_interval` at 1 or below, the minimum is one day more.

**Lapse.** The interval after the relearning pass is `max(minimum_interval, round(I × new_interval))`. With Anki's default `new_interval` of 0, that is 1 day: the concept starts growing again from 1 day, with its lower ease.

**Steps.** Anki's learning and relearning steps last minutes (1 and 10 minutes for new cards, 10 minutes after a lapse). Recall works in study days, so a step is the next day's question. Compared with Anki, this adds one review after each fail.

**Fuzz.** As in Anki, intervals under 2.5 days are never fuzzed. Longer ones may move either way by 1 day, plus 15% of the part of the interval between 2.5 and 7 days, 10% of the part between 7 and 20 days, and 5% of the part above 20 days: about ±1 day at 3 days, ±3 days at 20 and ±7 days at 100. The result stays within the minimum growth and `maximum_interval`. Set `fuzz` to `false` to turn it off.

**Example.** A concept answered on time, fuzz off:

- always pass: 1, 3, 8, 20, 50, 125, 313 days (ease 2.5);
- a lapse at 90 days: ease 2.3; after the next day's pass, 1, 3, 7, 16, 37, 85 days.

**Ease hell.** With only pass and fail, the ease can only go down: Anki raises it only on Easy. A concept that lapsed six times stays at the 1.3 floor and grows by about 30% per review for good. To avoid it, set `ease_penalty` to 0, which keeps every concept at the starting ease while keeping Anki's lapse rules, or use `thirds`.

**Not included.** FSRS, Anki's optional modern algorithm, isn't built in. It works best with parameters fitted to your review history, and `.srs/log.csv` keeps that history in case it is added later. Anki's load balancing and easy days aren't included either.

## thirds

Recall's own rules, built for pass and fail: no ease, a fixed multiplier, and a lapse that costs part of the interval once.

| State | Pass | Fail |
| --- | --- | --- |
| new, learning | review; interval 1 day | learning, back the next day |
| review | interval × `multiplier`, then fuzzed | a lapse: relearning, back the next day; pending interval = interval ÷ `lapse_divisor` |
| relearning | review; interval = the pending interval, fuzzed | another lapse: pending ÷ `lapse_divisor`, back the next day |

**Pass in review.** `new = max(I + 1, round(base × multiplier))`, where `base` is `I` when answered on time and `I + late / 2` when late; then fuzzed, never below `I + 1`.

**Lapses.** Every cut keeps at least `min_interval` days. Each fail after graduation counts as a lapse, including fails while relearning, which is why the default leech threshold is higher than Anki's.

**Fuzz.** Intervals of `fuzz_min_interval` days or more move by up to `fuzz` (5%) either way.

**Example.** A concept with a 90-day interval is failed. It comes back the next day; a pass sets the interval to 30 days. Failing again instead sets the pending interval to 10 days and brings it back the next day; then 3, then 1.

**Why.** A fixed multiplier avoids ease hell: after a lapse, the concept grows at the normal pace again. Keeping a third of the interval is gentler than Anki's restart at 1 day for a concept that was known for months and slipped once, and repeated fails still bring it down to 1 day.

## Calibrating

`python3 <plugin folder>/scripts/srs.py stats` reports the pass rate on reviews over the last 30 days. It counts only answers given in the review phase, not new, learning or relearning ones, and splits them by the interval before the answer: young under `scheduling.mature_interval` (21 days by default, as in Anki), mature at that interval or more. Each group shows its rate and count, or "not enough data" below 30 reviews; a combined rate follows.

Calibrate on mature retention once it has 30 reviews, and on young retention until then. Mature reviews test the long intervals that matter most, and they are less noisy than the first short ones. Aim for 85 to 90% (`target_retention`).

- Above 95%, reviews come too early: raise `interval_modifier` (anki) or `multiplier` (thirds).
- Below 80%, they come too late: lower it.

For `interval_modifier`, Anki's manual suggests multiplying it by `log(desired retention) / log(current retention)`: from 80% to 90%, `log(0.9) / log(0.8) ≈ 0.47`.

## Switching algorithms

Change `scheduling.algorithm`. Every concept keeps its state, interval and due date. Under `anki`, a concept without an ease starts at `starting_ease`. A concept in relearning keeps the pending interval set by the old algorithm.

## Adding an algorithm

1. Fork the repository.
2. In `scripts/srs.py`, write a function next to `schedule_anki` and `schedule_thirds`:

   ```python
   def schedule_mine(rec: dict, passed: bool, today: dt.date, p: dict, name: str) -> None:
       ...
   ```

   It receives the concept's record, the answer, the study day, the parameters (defaults merged with the vault's config) and the concept name. It updates the record in place: `phase` (one of the four states), `interval`, `pending`, `due` (an ISO date) and `lapses`, plus any field of its own, as `anki` stores `ease`. The shared code handles the rest: holds after new content, the daily list, repetitions, focus lines and leeches.
3. Give it default parameters, including `leech_threshold`, and register it in `ALGORITHMS` with a one-line summary for `/setup` and the parameter to adjust when retention is off target.
4. For any randomness, use `unit_random(name, day)`, so the same input always gives the same date.
5. Add tests next to `AnkiTest` and `ThirdsTest` in `tests/test_srs.py`, and run `python3 -m unittest discover -s tests`.
6. Push your fork and install it the same way as the original, with your GitHub user in place of the owner.

Set `"algorithm": "mine"` in `.srs/config.json`, and `"mine": {...}` for parameters that differ from the defaults.
