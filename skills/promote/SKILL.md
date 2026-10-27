---
name: promote
description: Move concepts that are only in the Recall index into the review queue, by page, topic or concept, or from a suggestion based on recent study.
disable-model-invocation: true
argument-hint: "[page, topic or concept]"
allowed-tools:
  - Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/srs.py *)
---

# Recall /promote

!`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/srs.py context promote $ARGUMENTS`

The block above comes from Recall's script. Call the script as `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/srs.py <command>`, written `srs.py <command>` below, on its own on the command line. Follow the rules at the end of the block.

## Before you start

- If the block says NOT SET UP, tell the user to run /setup, and stop.
- If it reports an INTERRUPTED SESSION, show what was left uncommitted and ask whether to keep or discard it; do what it says, then continue.
- If there are no candidates, say so and stop.

## Steps

1. Choose what to propose:
   - With a query, propose the matching candidates.
   - Without one, propose a batch from the pages most recently touched by /integrate, listed first; if there are none, ask what topic the user wants to promote.
   Size the batch to the queue: new concepts enter the daily quiz a few per day, so a long queue means a long wait. Say how long the queue line in the block says it would take.

2. Show one numbered list, each concept with a suggestion (review, or leave in the index) and a half-line reason. Suggest review for concepts that are core, foundational or subtle; suggest leaving author or book details, names and dates, and things one looks up in seconds. The user answers "ok" or adjusts.

3. Apply the answer: `srs.py begin promote`, then `srs.py add --source promote "<Concept>" "<Concept>" ...`, then `srs.py commit -m "promote: <N> concepts"`, then `srs.py end`.

4. Close with one line: how many concepts were queued and how long the queue now is.
