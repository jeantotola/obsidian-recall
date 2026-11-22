---
name: maintenance
description: Bring the Recall index and folder map up to date with the vault's pages, audit unverified pages with a cheap read-only agent, seal the pages that pass, and report what needs fixing.
disable-model-invocation: true
allowed-tools:
  - Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/srs.py *)
---

# Recall /maintenance

!`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/srs.py context maintenance`

The block above comes from Recall's script. Call the script as `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/srs.py <command>`, written `srs.py <command>` below, on its own on the command line. Follow the rules at the end of the block.

## Before you start

- If the block says NOT SET UP, tell the user to run /setup, and stop.
- If it reports an INTERRUPTED SESSION, show what was left uncommitted and ask whether to keep or discard it; do what it says, then continue.

## Steps

1. Run `srs.py begin maintenance`.

2. **Bring the index and the folder map up to date**, before the audit, which judges each page against the whole index. Skip this step without a word when the "To index" section says there is nothing to index and the folder map lacks no row. Indexing puts nothing in the schedule.
   1. **Entries without their page.** For each one the section lists, read the pages it may be about. With none listed, look for a page to index that explains the entry's concepts. When one is the same page renamed or moved, change the entry's link to it; for an ambiguous link, write the path of the page that explains the concepts. Page names aren't in the schedule, so this needs no `srs.py rename`. When no page fits, leave the entry for the report.
   2. **Folder map**, only when the block has a "## Folder map" heading. If the map doesn't exist, write it at the path the block gives, from the template in the block, in the language Recall talks to the user in. If it exists, only add the missing rows and leave the rest as it is; but when a row names no folder and a folder without a row holds what that row describes, the folder was renamed: change the name in the row and keep its scope. Write one row per listed folder, with the name exactly as listed. Take each scope from what the folder holds, its page names (read a page when the names leave it unclear), not from the folder name alone. In the last column, name the folder that takes what doesn't belong. Add one tie-break question for each pair of folders that could both take a page. Keep the section on new folders as the template has it. List pages that look misplaced in the chat, and don't move them: the scopes say where pages belong, not where the misplaced ones are.
   3. **Pages to index.** Take the batch from the block, or from `srs.py index-batch` if you changed a link or this isn't the first round. If it isn't empty, use the Agent tool with the `recall:index-drafter` agent. Give it the batch paths, the concept language, the description limit and the index path.
   4. Append its entries to the index file with Edit. Resolve duplicates by reading both pages. When the page in the index no longer explains the concept, it was moved: move its line to the new page's entry. When both explain it, keep one owner, preferring a page about the concept over a diary-style page, and remove it from the other entry. A page left with no concept gets no entry and counts as one with nothing to index.
   5. If nothing changed and no page has nothing to index, go on to step 3 without asking. Otherwise run `srs.py diff`. If it prints index warnings about this step's changes, fix them and run it again before asking for approval. Show the user the diff in a ```diff block, with one line each on renamed or moved pages, duplicates, and pages with nothing to index and why. For a new map, say in one line that the scopes, the tie-breaks and the numbers in the rules for new folders, such as the 3-page minimum for a new top-level folder, are theirs to edit in the file. Ask: ok, change, skip or stop.
   6. On "ok": run `srs.py index-skip "<path>" ...` for the pages with nothing to index, so they don't come back until they change, then `srs.py commit -m "maintenance: index <N> pages"`, with `, folder map` or `, fix index links` after it when they changed too, or alone when no page was indexed. With no entry to add and only pages to skip, ask about that list and use `"maintenance: <N> pages with nothing to index"`. On a change request: edit, then show the new diff. On "skip": `srs.py discard`, and go on with the index as it was: to step 3, or straight to step 4 when the skipped diff fixed the link of a renamed page, whose old seal the next run still needs. On "stop": `srs.py discard`, then `srs.py end`.
   7. If `srs.py index-batch` still has pages to index, say how many and ask: the next batch, the audit, or stop (`srs.py end`). A large vault takes many rounds, and the audit can wait for another day. Repeat from 2.3 for the next batch.

3. Run `srs.py prune-seals` without asking: it moves the seals of renamed pages, and removes the seals and flags of files that are gone or are no longer pages (excluded, the inbox, the index), the flags of concepts that left the index, and the open questions of pages that changed. Note what it moved and removed. It runs only now because step 2 recognizes a renamed page by its old seal.

4. If step 2 committed anything, or step 3 moved a seal, the block's batch to audit is out of date: run `srs.py audit-batch` and use its output. If the batch is not empty:
   1. Use the Agent tool with the `recall:page-auditor` agent. Give it the batch exactly as listed (each page with its absolute path, its index entry and, when it has one, its "Changed since its seal" section) and the absolute path of the index. Folder names may start with a marker such as `A) `: it is part of the name, and the absolute path must be used as given.
   2. Meanwhile, read the pages of the batch yourself and look for errors and typos, as the rules say. Compare the index descriptions with what the page says.
   3. The auditor's report is a list of leads. For each failure or note, open the page, and for a duplicate the other page too; run `srs.py show` on the concepts it names and on the names you would propose. Drop what you can't confirm and what an incomplete callout on the page already says: a dropped lead isn't in the report. A concept under "Removed from the index before", or that `show` says was removed, is already decided: don't propose it, and say so in one line.

5. Run `srs.py verify "<path>" "<path>" ...` with every page that has nothing left: no confirmed failure and no error. The script refuses pages that fail the size or shared-name checks; report those as failures. Notes and typos don't stop a seal. Then run `srs.py commit -m "maintenance: <N> pages verified"`, with `, <M> stale seals removed` after it when step 3 removed some, or `"maintenance: <M> stale seals removed"` when no page was sealed. With nothing changed since step 2, skip it.

6. Report in short sections, leaving out empty ones. Give every item that asks for a decision an ID that stays the same for the whole session: Q1, Q2… for open questions, E… for errors, F… for failures, D… for the deterministic checks, T… and M… for notes, G… for gaps, L… for leeches. The user answers by ID.
   - pages sealed;
   - stale seals, flags and open questions removed, and seals moved, with the count and the paths;
   - open questions: the ones the user postponed, on pages that haven't changed since. Show them as they are, without another audit;
   - errors, and the typos as one item;
   - pages that failed, with the criterion and the reason. For a duplicate (C or C?), say whether each concept is in the quiz and its interval: that is what removing one loses. For a C, give the fix the auditor chose: keep the concept on the owner page and drop it from the other, with `srs.py merge "<duplicate>" "<owner>"` if the duplicate is in the schedule, so the owner keeps a history. For a C?, say in one line what each page says (and what the applied page adds), and offer two options: one owner, with the other page mentioning the idea and linking to it, or keep both, the applied one with a qualifier in its name and a description of what it adds. When A fails because the page never explains the subject its title names, offer to add that subject to the entry once the page explains it;
   - problems from the deterministic checks (from `srs.py check`, if step 2 committed anything), with the fix for each: split pages over the size limit, rename or merge duplicate names, shorten long descriptions, fix links that don't resolve, remove or re-add orphans, and remove or rename folder-map rows that name no folder. Before removing an index entry whose page is gone, say which of its concepts are in the quiz (`srs.py show`), since they would be left without a page;
   - leeches: the concept may be too broad (split it) or its page confusing (rewrite it);
   - gaps that won't close: suggest rewriting that part of the page, or making the idea a concept of its own, in the index and the schedule, then running `srs.py gap "<Concept>" --close`;
   - this run's notes: for a T note, offer an incomplete callout; for an M note, ask whether to flag it: `srs.py flag "<page path>" --misplaced "<Concept>" --to "<where it belongs>"`. Leave the others alone;
   - concepts flagged as misplaced, with where they belong, and thin flags from before 0.26.0: offer the fix (step 7), and for a thin flag an incomplete callout. Misplaced flags show up only in this report, and each one stays until its page is fixed and unflagged;
   - pages still not in the index, and pages past this batch: how many.

   What each answer to an error or a failure does:
   - **corrected:** step 7, then `srs.py reseal` when the page had its seal, or `srs.py verify` when it didn't;
   - **no**, for a failure: `srs.py verify`, which seals the page as it is. For a duplicate, both pages keep or get their seals, except one that was never audited. A "no" to an error doesn't seal: ask about it alone, as the rules say, and without an explicit yes it is a not now;
   - **not now**, or no answer: `srs.py open "<page>" "<the question, with its passage>"`. The page waits without a seal, and isn't audited again until it changes. For a duplicate, open it on the failing page only.
   A page with several items is sealed when all of them are answered.

7. Never fix anything on your own. If the user asks for a fix (correct an error, split a page, merge or rename concepts, rewrite a page), make it with Edit, run `srs.py diff`, fix any index warnings it prints, show the diff and wait (an "ok" to an item of the report approves the fix, not its commit), and on "ok" seal as step 6 says and run `srs.py commit -m "maintenance: <what was fixed>"`. If the session is already closed when the user asks, open a new one first with `srs.py begin maintenance`. When a fix renames, merges or removes a concept that is in the schedule, also run `srs.py rename "<old>" "<new>"`, `srs.py merge "<duplicate>" "<owner>"` or `srs.py remove "<Concept>" ...` before the commit. To fix a misplaced concept, move its definition to the page where it belongs under its own heading, leave a link `[[Page#Concept]]` where it was, update the index, and run `srs.py unflag "<page>" --misplaced "<Concept>"`. The schedule keeps the concept's name, so no `srs.py rename` is needed when only the page changes. Use `srs.py unflag "<page>"` to drop a thin flag.
   - When the diff warns about files the user edited in Obsidian during the session, run `srs.py adopt "<path>" ...` for them without asking, and say so in one line. Read the user's edits in the diff and look for errors in them.
   - When the user renames a page in Obsidian, it rewrites the text of every link to it that had no alias. Look for sentences where the new title now reads wrong and propose an alias, `[[New title|old wording]]`; run `srs.py prune-seals` for the old path's seal and flags.

8. When answers sealed, flagged or left pages open without a fix to commit, run `srs.py commit -m "maintenance: <N> items settled"`. A rewrite the user approved that goes beyond a correction, such as a split, leaves its pages without a seal: offer `srs.py audit-batch "<path>" ...` for them, and go through steps 4 to 6 with its output.

9. If `srs.py audit-batch` still has pages, say how many and ask: the next batch, or stop. For the next one, repeat from 4. Each batch is decided before the next is audited.

10. Before closing, list the decisions by ID in four groups: done, declined, postponed, still open. Then run `srs.py end`.
