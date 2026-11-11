---
name: maintenance
description: Check the Recall index and pages, audit unverified pages with a cheap read-only agent, seal the pages that pass, and report what needs fixing.
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

1. Run `srs.py begin maintenance`, then `srs.py prune-seals` without asking: it removes the seals of files that are gone or are no longer pages (excluded, the inbox, the index). Note how many it removed and which.

2. If the batch to audit is not empty, use the Agent tool with the `recall:page-auditor` agent. Give it the batch exactly as listed (each page with its absolute path and its index entry) and the absolute path of the index. Folder names may start with a marker such as `A) `: it is part of the name, and the absolute path must be used as given.

3. Run `srs.py verify "<path>" "<path>" ...` with every page the auditor passed. The script refuses pages that fail the size or shared-name checks; report those as failures. For each duplicate (criterion C or C?) the auditor reports, run `srs.py unseal "<other page>"` too, so both pages stay unsealed until the duplicate is fixed. Notes don't stop a seal, so keep them as flags: for each T note, run `srs.py flag "<path>"`; an M note is flagged only on the user's approval (step 4).

4. Report in short sections, leaving out empty ones:
   - pages sealed;
   - stale seals removed, with the count and the paths;
   - pages that failed, with the criterion and the reason. For a duplicate (C), give the fix the auditor chose: keep the concept on the owner page and drop it from the other (with `srs.py remove` if it is in the schedule). For a borderline one (C?), offer two options and let the user pick: keep it only on the owner page, or keep both, the applied one with a qualifier in its name and a description of what it adds;
   - problems from the deterministic checks, with the fix for each: split pages over the size limit, rename or merge duplicate names, shorten long descriptions, fix links that don't resolve, remove or re-add orphans, add a folder-map row for each folder without one (/setup drafts the map when it doesn't exist) and remove or rename rows that name no folder;
   - leeches: the concept may be too broad (split it) or its page confusing (rewrite it);
   - gaps that won't close: suggest rewriting that part of the page, or making the idea a concept of its own, in the index and the schedule, then running `srs.py gap "<Concept>" --close`;
   - pages flagged as thin, adding what this run's T notes say each one leaves out;
   - concepts the auditor noted as misplaced (M) in this run, each with its page and where it belongs: ask once which ones to flag, and run `srs.py flag "<page path>" --misplaced "<Concept>" --to "<where it belongs>"` for each one the user approves; leave the others alone;
   - concepts flagged as misplaced, each with its page and where it belongs: offer to move the definition (step 6);
   - pages not in the index: suggest /setup to index them.

5. If nothing changed (no page sealed, unsealed or flagged, no stale seal removed), skip the commit and go to step 7. Otherwise run `srs.py commit -m "maintenance: <N> pages verified"`; when stale seals were removed, use `"maintenance: <N> pages verified, <M> stale seals removed"`, and `"maintenance: <M> stale seals removed"` when no page was sealed.

6. Never fix anything on your own. If the user asks for a fix (split a page, merge or rename concepts, rewrite a page), make it with Edit, run `srs.py diff`, fix any index warnings it prints, show the diff, and on "ok" run `srs.py commit -m "maintenance: <what was fixed>"`. If the session is already closed when the user asks, open a new one first with `srs.py begin maintenance`. When a fix renames or removes a concept that is in the schedule, also run `srs.py rename "<old>" "<new>"` or `srs.py remove "<Concept>"` before the commit. Use `srs.py unflag "<page>"` once a thin page is fixed. To fix a misplaced concept, move its definition to the page where it belongs under its own heading, leave a link `[[Page#Concept]]` where it was, update the index, and run `srs.py unflag "<page>" --misplaced "<Concept>"`. The schedule keeps the concept's name, so no `srs.py rename` is needed when only the page changes.

7. Run `srs.py end`.
