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

3. Run `srs.py prune-seals` without asking: it removes the seals and flags of files that are gone or are no longer pages (excluded, the inbox, the index), and the flags of concepts that left the index. Note how many it removed and which. It runs only now because step 2 recognizes a renamed page by its old seal.

4. If step 2 committed anything, the block's batch to audit is out of date: run `srs.py audit-batch` and use its output. If the batch is not empty, use the Agent tool with the `recall:page-auditor` agent. Give it the batch exactly as listed (each page with its absolute path and its index entry) and the absolute path of the index. Folder names may start with a marker such as `A) `: it is part of the name, and the absolute path must be used as given.

5. Run `srs.py verify "<path>" "<path>" ...` with every page the auditor passed. The script refuses pages that fail the size or shared-name checks; report those as failures. For each duplicate (criterion C or C?) the auditor reports, run `srs.py unseal "<other page>"` too, so both pages stay unsealed until the duplicate is fixed. Notes don't stop a seal, and they become flags only on the user's approval (step 6).

6. Report in short sections, leaving out empty ones. Give every item that asks for a decision an ID that stays the same for the whole session: F1, F2… for failures, D… for the deterministic checks, T… and M… for notes, G… for gaps, L… for leeches. The user answers by ID.
   - pages sealed;
   - stale seals and flags removed, with the count and the paths;
   - pages that failed, with the criterion and the reason. For a duplicate (C or C?), run `srs.py show` on both concepts and say whether each is in the quiz and its interval: that is what removing one loses. For a C, give the fix the auditor chose: keep the concept on the owner page and drop it from the other, with `srs.py merge "<duplicate>" "<owner>"` if the duplicate is in the schedule, so the owner keeps a history. For a C?, read the other page first, say in one line what each page says (and what the auditor says the applied page adds), and offer two options: one owner, with the other page mentioning the idea and linking to it, or keep both, the applied one with a qualifier in its name and a description of what it adds. When A fails because the page never explains the subject its title names, offer to add that subject to the entry once the page explains it;
   - problems from the deterministic checks (from `srs.py check`, if step 2 committed anything), with the fix for each: split pages over the size limit, rename or merge duplicate names, shorten long descriptions, fix links that don't resolve, remove or re-add orphans, and remove or rename folder-map rows that name no folder. Before removing an index entry whose page is gone, say which of its concepts are in the quiz (`srs.py show`), since they would be left without a page;
   - leeches: the concept may be too broad (split it) or its page confusing (rewrite it);
   - gaps that won't close: suggest rewriting that part of the page, or making the idea a concept of its own, in the index and the schedule, then running `srs.py gap "<Concept>" --close`;
   - this run's notes. Ask once which ones to flag: `srs.py flag "<page path>" --note "<what it leaves out>"` for a T note, `srs.py flag "<page path>" --misplaced "<Concept>" --to "<where it belongs>"` for an M note; leave the others alone;
   - pages flagged as thin, with their notes, and concepts flagged as misplaced, with where they belong: offer the fix (step 8). Add one line: flags show up only in this report, and each one stays until its page is fixed and unflagged;
   - pages still not in the index, and pages still without a seal past this run's batch: how many, for the next /maintenance.

7. If nothing changed since step 2 (no page sealed, unsealed or flagged, no stale seal or flag removed), skip the commit and go to step 9. Otherwise run `srs.py commit -m "maintenance: <N> pages verified"`; when stale seals were removed, use `"maintenance: <N> pages verified, <M> stale seals removed"`, and `"maintenance: <M> stale seals removed"` when no page was sealed.

8. Never fix anything on your own. If the user asks for a fix (split a page, merge or rename concepts, rewrite a page), make it with Edit, run `srs.py diff`, fix any index warnings it prints, show the diff, and on "ok" run `srs.py commit -m "maintenance: <what was fixed>"`. If the session is already closed when the user asks, open a new one first with `srs.py begin maintenance`. When a fix renames, merges or removes a concept that is in the schedule, also run `srs.py rename "<old>" "<new>"`, `srs.py merge "<duplicate>" "<owner>"` or `srs.py remove "<Concept>" ...` before the commit. Use `srs.py unflag "<page>"` once a thin page is fixed. To fix a misplaced concept, move its definition to the page where it belongs under its own heading, leave a link `[[Page#Concept]]` where it was, update the index, and run `srs.py unflag "<page>" --misplaced "<Concept>"`. The schedule keeps the concept's name, so no `srs.py rename` is needed when only the page changes.
   - When the diff warns about files the user edited in Obsidian during the session, run `srs.py adopt "<path>" ...` for them without asking, and say so in one line. Read the user's edits in the diff and point out any factual error in the chat, without fixing it.
   - When the user renames a page in Obsidian, it rewrites the text of every link to it that had no alias. Look for sentences where the new title now reads wrong and propose an alias, `[[New title|old wording]]`; run `srs.py prune-seals` for the old path's seal and flags.

9. Before closing, list the decisions by ID in four groups: done, declined, postponed, still open. If pages changed during the session, offer to audit them again: run `srs.py audit-batch "<path>" ...`, give its output to the auditor, seal and unseal as in step 5, and commit with `srs.py commit -m "maintenance: <N> pages verified"`. Then run `srs.py end`.
