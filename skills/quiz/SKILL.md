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

2. For each concept in today's list, in order:
   1. Read its page and find what the page says about the concept.
   2. Ask one question, following the quiz rules, with the count first ("3/14"). Wait for the answer.
   3. Judge it as the quiz rules say. If the user contests, their word wins.
   4. Run `srs.py record "<Concept>" pass` or `fail`, adding `--question "<the question in at most 15 plain words>"` with no quotes, backticks or dollar signs inside. Mention its output only when it reports a leech.
   5. If the page is too thin for a good question, ask what it allows and run `srs.py flag "<page path>"`.
   Skip a concept whose page line says to skip it, and tell the user.

3. After the list, ask again each concept that failed today, with a new question from another angle. Don't record these.

4. Run `srs.py today`. Tell the user the score, any leeches (the concept may be too broad or its page confusing: suggest /maintenance) and any page you flagged, in two or three lines.

5. Run `srs.py commit -m "quiz: <score>"` with the score from `srs.py today`, then `srs.py end`.

If the user stops early, go to step 4 with what was answered.
