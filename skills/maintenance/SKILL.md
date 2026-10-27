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

1. Run `srs.py begin maintenance`.

2. If the batch to audit is not empty, use the Agent tool with the `recall:page-auditor` agent. Give it the batch exactly as listed (each path with its index entry) and the index path.

3. Run `srs.py verify "<path>" "<path>" ...` with every page the auditor passed. The script refuses pages that fail the size or shared-name checks; report those as failures. For each duplicate (criterion C) the auditor reports, run `srs.py unseal "<other page>"` too, so both pages stay unsealed until the duplicate is fixed.

4. Report in short sections, leaving out empty ones:
   - pages sealed;
   - pages that failed, with the criterion and the reason;
   - problems from the deterministic checks, with the fix for each: split pages over the size limit, rename or merge duplicate names, shorten long descriptions, fix links that don't resolve, remove or re-add orphans;
   - leeches: the concept may be too broad (split it) or its page confusing (rewrite it);
   - pages flagged as thin by /quiz;
   - pages not in the index: suggest /setup to index them.

5. Run `srs.py commit -m "maintenance: <N> pages verified"`.

6. Never fix anything on your own. If the user asks for a fix (split a page, merge or rename concepts, rewrite a page), make it with Edit, run `srs.py diff`, show the diff, and on "ok" run `srs.py commit -m "maintenance: <what was fixed>"`. If the session is already closed when the user asks, open a new one first with `srs.py begin maintenance`. When a fix renames or removes a concept that is in the schedule, also run `srs.py rename "<old>" "<new>"` or `srs.py remove "<Concept>"` before the commit. Use `srs.py unflag "<page>"` once a thin page is fixed.

7. Run `srs.py end`.
