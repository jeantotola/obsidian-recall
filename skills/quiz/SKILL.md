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

1. Run `srs.py begin quiz`. It turns off suggested replies in the input line, which can show the answers, until the session ends. If it prints a note for the user, relay it in their language before the first question.

2. For each concept in today's list, in order:
   1. Read its page and find what the page says about the concept.
   2. Ask one question, following the quiz rules, with the count first, starting from the list's Question count line ("11/15"). A skipped concept still advances the count. Wait for the answer. When the concept has a Gap line, the question tests that idea, in different wording or applied to a case, whatever the recent questions were. If the page no longer supports the idea, run `srs.py gap "<Concept>" --close`, say so in one line and ask as usual.
   3. Judge it as the quiz rules say. If the user contests, their word wins (see Contesting a verdict). When the answer is right about the concept but misses or gets wrong only a prerequisite that has its own concept in the index, judge the concept by its own idea, and offer to open a gap on the prerequisite: `srs.py gap "<Prerequisite>" "<the idea the answer missed>"`. When the prerequisite isn't in the review schedule, ask first whether to add it (`srs.py add --source promote "<Prerequisite>"`). When the answer misses both, the concept fails as usual and the gap on the prerequisite is offered too. Open nothing the user doesn't approve.
   4. Run `srs.py record "<Concept>" pass` or `fail`, adding `--question "<the question in at most 15 plain words>"` with no quotes, backticks or dollar signs inside. On a fail, also add `--gap "<the idea the answer missed, in at most 15 plain words, in the user's language>"`, with the same limits: name what was missing or wrong, not the topic. Mention its output only when it reports a leech or a closed gap, in one line.
   5. If the page is too thin for a good question, ask what it allows and run `srs.py flag "<page path>"`.
   6. If the concept is defined on a page whose subject isn't the concept's own, say so in one line and ask whether to flag it. Only on approval, run `srs.py flag "<page path>" --misplaced "<Concept>" --to "<page or subject where it belongs>"`.
   Skip a concept whose page line says to skip it, and tell the user.

3. After the list, ask again each concept that failed today, without a count. Test the idea the answer missed, in different wording or applied to a case; don't move to another fact of the concept, and don't put the answer in the question. Don't record these.

4. Run `srs.py today`. Tell the user the score, any leeches (the concept may be too broad or its page confusing: suggest /maintenance) and any page or concept you flagged, in two or three lines.

5. Run `srs.py commit -m "quiz: <score>"` with the score from `srs.py today`, then `srs.py end`.

## Contesting a verdict

When the user contests a verdict, at any moment of the session, run `srs.py amend "<Concept>" pass` or `fail` with the result they stand for, adding `--gap "<the idea the answer missed>"` when amending to fail, and confirm in one line. A concept amended to pass leaves the repeat at the end of the session; amended to fail, it joins it. The score still comes from `srs.py today`. If amend refuses, tell the user what it says.

When the user says a gap is wrong, or asks to close it, run `srs.py gap "<Concept>" --close`.

If the user stops early, go to step 4 with what was answered.
