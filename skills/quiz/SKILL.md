---
name: quiz
description: Today's Recall spaced-repetition quiz. The script builds the list; Claude asks one question per concept from its page, judges the answer and records pass or fail.
disable-model-invocation: true
allowed-tools:
  - Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/srs.py *)
---

# Recall /quiz

!`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/srs.py context quiz`

The block above comes from Recall's script. Call the script as `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/srs.py <command>`, written `srs.py <command>` below, on its own on the command line. Follow the quiz rules and the rules in the block.

## Before you start

- If the block says NOT SET UP, tell the user to run /setup, and stop.
- If it reports an INTERRUPTED SESSION, show what was left uncommitted and ask whether to keep or discard it; do what it says, then continue.
- If there is nothing to ask today, say so and when the next review is due, and stop.

## Steps

1. Run `srs.py begin quiz`.

2. Prepare the questions in the background, in this same turn, before asking anything:
   1. Leave concept 1 out. Split the rest of today's list, in order, into batches of up to 5 concepts. Concepts with the same owner page always go in the same batch, even if that makes it larger than 5: add each concept to the batch that already holds its page, and open a new batch when the current one has 5 or more. Leave out any concept whose page line says to skip it, and tell the user.
   2. Launch one `recall:question-drafter` agent per batch with the Agent tool, in the background (`run_in_background`), all in one message. Give each the path of `.srs/quiz-rules.md`, the language to ask in, and for each concept in its batch: the name, the owner page, the focus and the recent questions.
   3. Then read concept 1's page and ask its question yourself, as in step 3. If there is only one concept, launch nothing.

   When a batch finishes, write nothing to the user; keep its result. If a batch fails or never arrives, ask its concepts yourself, as in step 3.

3. For each concept in today's list, in order:
   1. If its batch has arrived, use the drafted QUESTION. Otherwise read its page, find what it says about the concept and formulate one question yourself. Either way follow the quiz rules, and put the count first ("3/14"). Wait for the answer.
   2. Judge it as the quiz rules say, against the drafted ANSWER when you have one. Read the page only if the user contests. If the user contests, their word wins.
   3. Run `srs.py record "<Concept>" pass` or `fail`, adding `--question "<the question in at most 15 plain words>"` with no quotes, backticks or dollar signs inside. Mention its output only when it reports a leech.
   4. If the drafter marked the page THIN, or the page is too thin for a good question when you ask it yourself, run `srs.py flag "<page path>"`.

4. After the list, ask again each concept that failed today, with a new question from another angle: the drafted SECOND when you have one, otherwise one you formulate. Don't record these.

5. Run `srs.py today`. Tell the user the score, any leeches (the concept may be too broad or its page confusing: suggest /maintenance) and any page you flagged, in two or three lines.

6. Run `srs.py commit -m "quiz: <score>"` with the score from `srs.py today`, then `srs.py end`.

If the user stops early, go to step 5 with what was answered.
