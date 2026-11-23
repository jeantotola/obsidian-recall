---
name: setup
description: Set up Recall, a git-native study system, in this Obsidian vault. Run once; /maintenance then indexes the vault's existing pages.
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
   - the inbox, index, new-pages and folder-map paths (defaults from the block; mention any name conflict it lists);
   - where `/integrate` puts new pages: `folders` (the default; each new page goes in the existing folder whose topic fits best, and the new-pages path is only the fallback) or `flat` (every new page goes in the new-pages path). Suggest what the block suggests: `folders` when the vault already has folders with pages;
   - the heading level that opens an inbox block (default 2; if the inbox already exists, suggest the level its headings use);
   - folders or patterns to exclude (suggest the detected ones). Patterns: `folder/` for a folder, `*.excalidraw.md` for file names anywhere;
   - whether they want the daily quiz (default on): a spaced-repetition quiz on the concepts they choose to remember. Without it, Recall keeps the inbox, the index and the pages in order, and /config turns the quiz on later;
   - only with the quiz on, the scheduling algorithm, from the list in the block, one line each: `anki` (the default) or `thirds`. Mention that the Scheduling section of Recall's manual, `docs/usage.md`, compares them and that it can be changed later in `.srs/config.json`.
3. Run `srs.py init` with the answers: `--quiz <on|off> --algorithm <name> --language "..." --concept-language "..." --inbox "..." --index "..." --new-pages "..." --folder-map "..." --new-page-placement <folders|flat> --heading-level N`, one `--exclude "..."` per pattern, `--algorithm` only with the quiz on, and `--git-init` only if the folder has no repository and the user agreed to create one.
4. If it reports a missing git identity, show the two commands it prints and stop; the user runs them and then /setup again.
5. Close with what comes next: relay the number of pages not in the index that `srs.py init` printed and, when there are any, say that /maintenance drafts the folder map (with `folders`) and indexes them, one batch at a time, and that only the concepts they pick go into the quiz. From now on /integrate takes care of the inbox and /maintenance of the wiki. Don't index anything here.

## If Recall is already configured

1. If the block reports an INTERRUPTED SESSION, resolve it first as it says.
2. Say that Recall is set up and that /setup has nothing left to do: /maintenance indexes pages and keeps the folder map (give the number of pages not in the index), and /config changes the settings. Stop.
