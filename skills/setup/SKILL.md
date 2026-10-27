---
name: setup
description: Set up Recall, a git-native study system, in this Obsidian vault, or continue indexing the vault's existing pages. Run once, then again until every page is indexed.
disable-model-invocation: true
allowed-tools:
  - Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/srs.py *)
---

# Recall /setup

!`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/srs.py context setup`

The block above comes from Recall's script. Call the script as `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/srs.py <command>`, written `srs.py <command>` below, on its own on the command line. Follow the rules at the end of the block.

## If Recall is NOT CONFIGURED

1. In the language the user writes in, say what /setup will do in this folder: create `.srs/`, the inbox and the index, add a few lines to `.gitignore`, and commit. If the folder is not a git repository, say Recall is git-native and /setup will create one.
2. Ask everything in one message, showing the defaults, and accept "ok" as "defaults and suggestions":
   - the language Recall should talk to them in;
   - the language for concept names (default English);
   - the inbox, index and new-pages paths (defaults from the block; mention any name conflict it lists);
   - where `/integrate` puts new pages: `folders` (the default; each new page goes in the existing folder whose topic fits best, and the new-pages path is only the fallback) or `flat` (every new page goes in the new-pages path). Suggest what the block suggests: `folders` when the vault already has folders with pages;
   - the heading level that opens an inbox block (default 2; if the inbox already exists, suggest the level its headings use);
   - folders or patterns to exclude (suggest the detected ones). Patterns: `folder/` for a folder, `*.excalidraw.md` for file names anywhere;
   - the scheduling algorithm, from the list in the block, one line each: `anki` (the default) or `thirds`. Mention that the README's Scheduling section compares them and that it can be changed later in `.srs/config.json`.
3. Run `srs.py init` with the answers: `--algorithm <name> --language "..." --concept-language "..." --inbox "..." --index "..." --new-pages "..." --new-page-placement <folders|flat> --heading-level N`, one `--exclude "..."` per pattern, and `--git-init` only if the folder has no repository and the user agreed to create one.
4. If it reports a missing git identity, show the two commands it prints and stop; the user runs them and then /setup again.
5. Continue with indexing. The block above was computed before setup, so get the first batch from `srs.py setup-batch`.

## Indexing existing pages

Nothing enters the review queue during setup. Each round indexes one batch.

1. If the block reports an INTERRUPTED SESSION, resolve it first as it says.
2. If every page is indexed, say so, suggest /integrate or /promote, and stop.
3. `srs.py begin setup`.
4. Use the Agent tool with the `recall:index-drafter` agent. Give it the batch paths (from the block, or from `srs.py setup-batch` after the first round), the concept language, the description limit and the index path.
5. Append its entries to the index file with Edit. Resolve duplicates: when two pages explain the same concept, keep one owner, preferring a page about the concept over a diary-style page, and remove the concept from the other entry.
6. Run `srs.py diff` and show the user the diff in a ```diff block, with one line on duplicates or skipped pages. Ask: ok, change or stop.
7. On "ok": `srs.py commit -m "setup: index <N> pages"`. On a change request: edit, then show the new diff. On "stop": `srs.py discard`.
8. Say how many pages are left and ask whether to index the next batch (`srs.py setup-batch` lists it). When the user stops or nothing is left: `srs.py end`.
