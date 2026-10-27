---
name: integrate
description: Integrate the Recall inbox into the vault's pages and index. Plan the whole batch first, then go block by block, with a git diff to approve before each commit. Use at the end of a study session.
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

1. Run `srs.py begin integrate`. In one short message, tell the user how many blocks there are and repeat the queue line from the block. The blocks to process are the newest ones, at most the number in "Process up to".

2. Plan the batch before writing anything.
   1. Run `srs.py inbox show <id> <id> ...` once, with the ids of every block to process, newest first. Each block comes labelled with its id, so blocks with the same title stay apart. Read them all.
   2. Group the blocks that deal with the same subject, even when their titles are equal or vague. A block that mixes subjects can be split between groups; say so in the list.
   3. Use the index above to find every page that could relate to each group, and read those pages.
   4. Decide where each group goes: an existing page; a new page; or sections of one page, one section per block, when the blocks belong together. There is no minimum page size, so a group can be one short page or one section. Place a new page as the block's placement line says. With `folders`, use the folder list to pick the existing folder whose topic fits best; if none fits, propose a new folder that follows the vault's naming convention (for example the next prefix, "G) ", when the folders are "A) ...", "B) ..."); use the new-pages folder only when the vault has no thematic folders. With `flat`, use the new-pages folder.
   5. Show a short numbered list, one line per group: its blocks (title and id) and where it goes, with the folder for a new page and the new folder marked as such. Groups run from the newest to the oldest, a group counting as its newest block; inside a group the blocks run from the oldest to the newest, so a page follows the order in which the user learned the subject. Ask, in the user's language, for "ok" or adjustments, and accept any clear equivalent. Moves, merges and splits change the list: show it again until it is approved. Write nothing in this step. If the user stops, run `srs.py end` and stop.

3. For each group in the plan, and inside it for each block from the oldest to the newest:
   1. `srs.py inbox show <id>`.
   2. Read the pages you haven't read yet.
   3. Write the changes the plan calls for with Edit or Write. A block that continues the subject of a page created or edited in this session becomes a section of that page, not a new page, unless the plan says otherwise. Keep the user's wording; move, link and adjust as little as possible; put anything you add on your own in a `> [!claude]` callout; never pad with textbook content. When the block contains a factual error, keep it and point it out in the chat.
   4. Update the index for every concept the block now gives a page, following the index rules. A concept already owned by another page stays there; link to it instead.
   5. Run `srs.py diff` and show the user the diff in a ```diff block, copied from its output, followed by one or two lines on what you did and any error you noticed. For a new page, say in one line which folder the plan chose (and that it is a new folder, if it is). If the user answers "move to X", move the page there, fix the links and the index, and show the new diff. Ask, in the user's language, whether to approve, change, skip or stop; accept any clear equivalent.
   6. On "ok": `srs.py inbox remove <id>`, then `srs.py commit -m "integrate: <block title>"`.
      On a change request: edit again and show the new diff.
      On "skip": `srs.py discard`; the block stays in the inbox. A page or section the skipped block was to create no longer exists, so the next block of its group takes over that role; say so in one line.
      On "stop": `srs.py discard` if anything is uncommitted, then go to step 4.

4. Review list. In one numbered list, show every concept you touched and suggest, with a half-line reason each:
   - new concepts: review, or index only;
   - concepts that were only in the index (unmarked): promote to review, or not.
   Suggest review for concepts that are core to what the user is studying now, foundations for other concepts, subtle distinctions, or things the block got wrong. Suggest index only for author or book details, names and dates, things one looks up in seconds, and peripheral facts. If the queue line says the queue is long, suggest fewer and say why. Also name the concepts marked `[review]` that received new content: they are updated automatically. The user answers "ok" or adjusts, for example "ok, without 3 and with 5".

5. Apply the answer: `srs.py add --source integrate "<Concept>" "<Concept>" ...` for the chosen concepts, and `srs.py update "<Concept>" --focus "<one line on what is new>"` for each `[review]` concept with new content. Then `srs.py commit -m "integrate: queue <N> concepts"` ("1 concept" when there is one). If nothing was chosen or updated, skip the commit.

6. Run `srs.py end`, and close with one line: blocks integrated, blocks left, concepts queued.
