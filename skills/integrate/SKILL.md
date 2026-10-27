---
name: integrate
description: Integrate the Recall inbox into the vault's pages and index, block by block, with a git diff to approve before each commit. Use at the end of a study session.
disable-model-invocation: true
argument-hint: "[number of blocks | file with blocks]"
allowed-tools:
  - Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/srs.py *)
---

# Recall /integrate

!`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/srs.py context integrate $ARGUMENTS`

The block above comes from Recall's script. Call the script as `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/srs.py <command>`, written `srs.py <command>` below, on its own on the command line. Follow the rules at the end of the block.

## Before you start

- If the block says NOT SET UP, tell the user to run /setup, and stop.
- If it reports an INTERRUPTED SESSION, show what was left uncommitted and ask whether to keep or discard it; do what it says, then continue.
- If the source has no blocks, say so, and stop.
- When the source is a file other than the inbox, add `--file "<path>"` to every `srs.py inbox` command.

## Steps

1. Run `srs.py begin integrate`. In one short message, tell the user how many blocks there are and repeat the queue line from the block. Then start with the newest block, and process at most the number in "Process up to".

2. For each block, newest first:
   1. `srs.py inbox show <id>`.
   2. Use the index above to find every page that could relate to the block, and read those pages.
   3. Decide where each part of the block belongs: a section of an existing page, or a new page in the new-pages folder. Write the changes with Edit or Write. Keep the user's wording; move, link and adjust as little as possible; put anything you add on your own in a `> [!claude]` callout; never pad with textbook content. When the block contains a factual error, keep it and point it out in the chat.
   4. Update the index for every concept the block now gives a page, following the index rules. A concept already owned by another page stays there; link to it instead.
   5. Run `srs.py diff` and show the user the diff in a ```diff block, copied from its output, followed by one or two lines on what you did and any error you noticed. Ask, in the user's language, whether to approve, change, skip or stop; accept any clear equivalent.
   6. On "ok": `srs.py inbox remove <id>`, then `srs.py commit -m "integrate: <block title>"`.
      On a change request: edit again and show the new diff.
      On "skip": `srs.py discard`; the block stays in the inbox.
      On "stop": `srs.py discard` if anything is uncommitted, then go to step 3.

3. Review list. In one numbered list, show every concept you touched and suggest, with a half-line reason each:
   - new concepts: review, or index only;
   - concepts that were only in the index (unmarked): promote to review, or not.
   Suggest review for concepts that are core to what the user is studying now, foundations for other concepts, subtle distinctions, or things the block got wrong. Suggest index only for author or book details, names and dates, things one looks up in seconds, and peripheral facts. If the queue line says the queue is long, suggest fewer and say why. Also name the concepts marked `[review]` that received new content: they are updated automatically. The user answers "ok" or adjusts, for example "ok, without 3 and with 5".

4. Apply the answer: `srs.py add --source integrate "<Concept>" "<Concept>" ...` for the chosen concepts, and `srs.py update "<Concept>" --focus "<one line on what is new>"` for each `[review]` concept with new content. Then `srs.py commit -m "integrate: queue <N> concepts"` ("1 concept" when there is one). If nothing was chosen or updated, skip the commit.

5. Run `srs.py end`, and close with one line: blocks integrated, blocks left, concepts queued.
