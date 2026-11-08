---
name: integrate
description: Integrate the Recall inbox into the vault's pages and index. Plan the whole batch first and have the plan approved, then go block by block, either with a git diff for the user to approve before each commit or, in agent review mode, with judge agents checking each block. Use at the end of a study session.
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
- The block says `Review mode: human` or `agent`. It decides how step 3 ends: the user approves each diff (human), or judge agents check each block and the user supervises only the blocks they send back (agent). The plan in step 2 is approved by the user in both modes.
- When the source is a file other than the inbox, add `--file "<path>"` to every `srs.py inbox` command, and to `srs.py review prepare` and `srs.py review park`.

## Steps

1. Run `srs.py begin integrate`. In one short message, tell the user how many blocks there are and repeat the queue line from the block. The blocks to process are the newest ones, at most the number in "Process up to".

2. Plan the batch before writing anything.
   1. Run `srs.py inbox show <id> <id> ...` once, with the ids of every block to process, newest first. Each block comes labelled with its id, so blocks with the same title stay apart. Read them all.
   2. Group the blocks that deal with the same subject, even when their titles are equal or vague. A block that mixes subjects can be split between groups; say so in the list.
   3. Use the index above to find every page that could relate to each group, and read those pages.
   4. Decide where each group goes: an existing page; a new page; or sections of one page, one section per block, when the blocks belong together. There is no minimum page size, so a group can be one short page or one section. Place a new page as the block's placement line says. With `folders` and a folder map in the block, follow the map: put the page in the folder whose row takes it, settle doubts with its tie-breaks, and propose a new folder only as its rules for new folders allow; a new top-level folder gets its row in the map, written in the same diff as its first page. Without a map, use the folder list to pick the existing folder whose topic fits best. Either way, name a new folder by the vault's convention (for example the next prefix, "G) ", when the folders are "A) ...", "B) ..."), and use the new-pages folder only when the vault has no thematic folders. With `flat`, use the new-pages folder. A new concept belongs to the page of its own subject, not of the block's. Test: can the concept be defined without mentioning the page's subject (PATH, in a page about virtual environments)? If so, it belongs on another page. Look first for an existing page on its subject and prefer it; propose a new page only when none fits. Then split the block: the definition goes to that page, and what stays in the original block is the part that uses the concept, with a link to it (`[[Page#Concept]]`). Say each split in the list. This applies only to the text being added; text already on a page moves only as an item of the plan says (item 5).
   5. Decide where each block goes inside its page, in didactic order: what a block builds on comes before it. In a new page, that is the order of its sections. In an existing page, it is the exact point, inside a section or between two, for example "after the section X". Text already on the page stays where it is, unless a block needs it moved or rewritten (to give a concept its own section, say). Then make that its own item of the list, naming the page and the section: "move <what> from <page> to <page>" or "rewrite <section> of <page>: <what changes>". Without such an item, existing text is not touched.
   6. Show a short numbered list, one line per group: where it goes, with the folder for a new page and the new folder marked as such, then its blocks (title and id) in didactic order, each with its position. Groups run from the newest to the oldest, a group counting as its newest block. Ask, in the user's language, for "ok" or adjustments, and accept any clear equivalent. Moves, merges, splits and new positions change the list: show it again until it is approved. Write nothing in the vault in this step. In agent mode, save every version of the list with `srs.py plan save "<line>" "<line>" ...` (one argument per line of the list, exactly as shown) before you ask, and show the user the script's output as the list: the last list saved is the one the judges check against. In agent mode the list also includes the blocks under "Blocks waiting for supervision", marked "(supervision)"; in human mode they are ordinary blocks. If the user stops, run `srs.py end` and stop.

3. For each group in the plan, and inside it for each block in the plan's didactic order (in agent mode, skip the blocks marked "(supervision)": step 3b handles them). Do 1 to 4 for each block; then 5 and 6 in human mode, or 7 in agent mode.
   1. `srs.py inbox show <id>`.
   2. Read the pages you haven't read yet.
   3. Write the changes the plan calls for with Edit or Write, putting the block at the position the plan gives it, not at the end of the page by default. Never move or rewrite the text already on the page unless an item of the approved plan says to, and then only what it names; otherwise only add to it. A block that continues the subject of a page created or edited in this session becomes a section of that page, not a new page, unless the plan says otherwise. Give each concept its own heading on its owner page, and link to it from other pages as `[[Page#Concept]]`. Keep the user's wording; move, link and adjust as little as possible; put anything you add on your own in a `> [!claude]` callout; never pad with textbook content. When the block contains a factual error, keep it and point it out in the chat.
   4. Update the index for every concept the block now gives a page, following the index rules. A concept already owned by another page stays there; link to it instead.
   5. (Human mode.) Run `srs.py diff`. If it prints index warnings, fix them and run it again before asking for approval. Show the user the diff in a ```diff block, copied from its output, followed by one or two lines on what you did and any error you noticed. For a new page, say in one line which folder the plan chose (and that it is a new folder, if it is). If the user answers "move to X", move the page there, fix the links and the index, and show the new diff. Ask, in the user's language, whether to approve, change, skip or stop; accept any clear equivalent.
   6. (Human mode.) On "ok": `srs.py inbox remove <id>`, then `srs.py commit -m "integrate: <block title>"`.
      On a change request: edit again and show the new diff.
      On "skip": `srs.py discard`; the block stays in the inbox. A page or section the skipped block was to create no longer exists, so the next block of its group in the plan takes over that role; say so in one line.
      On "stop": `srs.py discard` if anything is uncommitted, then go to step 4.
   7. (Agent mode.) The user does not approve this diff; the script and the judges check the block.
      1. `srs.py review prepare <id>`. On `Checks: FAIL`, fix what it lists and run it again; if it fails a second time, park the block (item 4 below).
      2. Call the agents named on its `Judges:` line with the Agent tool, all in one message so they run in parallel. The prompt for each is exactly `Input: <the Input path>`; add nothing, because they read that file themselves.
      3. `srs.py review verdict <id>`, and act on its last line:
         - `Review: PASS`: `srs.py inbox remove <id>`, then `srs.py commit --review <id> -m "integrate: <block title>"`.
         - `Review: REVISE`: the judges point at problems; you decide what to do about them. Change what you agree with, or keep the text and say why. Do this once. Then run `srs.py review final <id> --note "<one line: what you changed, or why you disagree>"`. On `Checks: ok`, remove the block and commit as above; on `Checks: FAIL`, park it.
         - `Review: MISSING ...`: park it, with the reason "no verdict captured".
      4. To park a block: `srs.py review park <id> --reason "<why, in one line>"`. It keeps the proposal and restores the files. A block of the same group that needed the page or section this one was to create is parked with `srs.py review park <id> --waiting --reason "waits for <title>"` without being written; other groups go on.
      5. One line in the chat per block: committed, with the verdicts, or parked, with the reason. If the command refuses a commit, read its message.

3b. Blocks that need supervision (agent mode, only when some were parked in this round or were already waiting). List them, one line each with the title and the reason, and ask, in the user's language, whether to look at them now or later. Later: leave them in the inbox; they stay marked for the next /integrate. Now: for each one, run `srs.py review restore <id>` and handle it as in human mode (items 5 and 6), whatever the mode. If the script says the patch no longer applies, or there is no saved proposal, write the block again as in item 3. A block that waited for another one goes after it.

4. Review list. In one numbered list, show every concept you touched and suggest, with a half-line reason each:
   - new concepts: review, or index only;
   - concepts that were only in the index (unmarked): promote to review, or not.
   Suggest review for concepts that are core to what the user is studying now, foundations for other concepts, subtle distinctions, or things the block got wrong. Suggest index only for author or book details, names and dates, things one looks up in seconds, and peripheral facts. If the queue line says the queue is long, suggest fewer and say why. Also name the concepts marked `[review]` that received new content: they are updated automatically. The user answers "ok" or adjusts, for example "ok, without 3 and with 5".

5. Apply the answer: `srs.py add --source integrate "<Concept>" "<Concept>" ...` for the chosen concepts, and `srs.py update "<Concept>" --focus "<one line on what is new>"` for each `[review]` concept with new content. Then `srs.py commit -m "integrate: queue <N> concepts"` ("1 concept" when there is one). If nothing was chosen or updated, skip the commit.

6. Run `srs.py end`, and close with one line: blocks integrated, blocks left, concepts queued; in agent mode, also how many blocks wait for supervision.
