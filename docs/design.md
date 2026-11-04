# Recall design

This document holds the full rules. The [README](../README.md) covers installation and daily use.

Recall turns an Obsidian vault into a study system with three moving parts: an inbox you write in, an index of the concepts each page explains, and a spaced-repetition schedule over the concepts you choose to remember. Six commands drive it. The model writes, asks and judges; a deterministic script keeps the state, does the arithmetic and runs git; you approve.

## Principles

1. **Approving must be cheap.** Claude proposes and you answer "ok"; discussion is the exception. Any inbox block may wait for another day. A system that is costly to maintain gets abandoned.
2. **The concept is the key.** Concept names link the index, the schedule and the pages, so they are unique and stable.
3. **The voice is yours.** Claude moves, links and adjusts your text as little as possible, and marks its own additions with a `> [!claude]` callout. It points out factual errors in the chat instead of fixing them silently, and never pads pages with textbook content.
4. **State and arithmetic belong to the script.** The model never writes dates and never edits `.srs/`; it calls `srs.py`. A hook enforces this.
5. **Integration ignores page size, and places pages by the configured placement.** With `new_page_placement` set to `folders` (the default), a new page goes in the existing folder whose topic fits best, or in a new folder that follows the vault's naming convention; the new-pages folder is the fallback for vaults with no thematic folders. With `flat`, every new page goes in the new-pages folder. Page size is checked by `/maintenance`. Integration plans the whole batch before it writes anything, so blocks on one subject end up together and no minimum page size is needed.
6. **Git-native.** Every operation ends in a commit, and no content is committed without your approval of its diff.

## Index and concepts

```text
## [[Eigendecomposition]]
- Eigenvectors
- Eigenvalues
- Reversibility (calculus) — integral and derivative as inverses
```

- **One owner per concept.** A page lists the concepts it explains, not the ones it mentions. Other pages link to the owner, for example with `[[Page#Heading]]`.
- **Unique names.** A name appears once in the whole index. When it is ambiguous, the qualifier goes in the name itself, in parentheses.
- **One language.** Names and descriptions use the configured concept language (English by default); Claude maps synonyms and translations onto it.
- **Optional descriptions.** Only when the name alone is ambiguous, after " — ", within 60 characters by default. A description clarifies; it doesn't define.
- **Glossary granularity.** A concept is what would get its own glossary entry: "Eigenvectors", not "Linear algebra" or "Eigenvectors of a diagonal matrix".
- **Schedule inside the index.** Every concept in the schedule exists in the index under the same name. The reverse doesn't hold: most indexed concepts may never be scheduled.
- **Renames go through the script.** Renaming or removing a scheduled concept in the index also requires `srs.py rename` or `srs.py remove`, which keep its history.
- **Obsidian links.** `[[Page]]` resolves as in Obsidian: by file name when it is unique, by the path from the vault root otherwise. An ambiguous link is reported by `/maintenance`.

Lines the parser doesn't understand inside a page section, and concepts outside any section, are reported. Text outside sections, headings other than `## [[...]]`, comments and fenced code are ignored.

## Inbox

The inbox (default `inbox.md`) is the single page you write in, one block per subject.

- A block starts at a heading of the configured level (default 2) and runs to the next heading of that level or higher. Headings inside code fences don't count. Text before the first block is kept and never processed.
- New blocks go at the end; `/integrate` runs the groups of blocks from the end backwards, so today's notes come first.
- Integrated blocks leave the inbox in the same commit as their integration; the original text stays in git history.
- Skipped blocks stay where they are.
- The inbox is excluded from indexing and from every check, whatever its size.

Each block has an id derived from its text, so its id changes if you edit it. `srs.py inbox remove` refuses an id it can't find, which protects a block you edited during a session.

## /integrate

`/integrate` moves the inbox into the vault. `/integrate 3` processes only the three newest blocks; `/integrate <file>` integrates another file block by block, such as an old diary page.

1. The script commits your manual edits (`vault: manual edits`), opens a session, and reports the new-concept queue: how many concepts wait and how many days that is at the current pace.
2. **Plan.** Before writing anything, Claude reads all the blocks it will process (`srs.py inbox show` with several ids labels each block by its id, so equal titles stay apart) and groups the blocks that deal with the same subject, even when their titles are equal or vague. The index points to the candidate pages, and Claude reads them. It then shows a short numbered list, one line per group: an existing page, a new page with its folder, or sections of one page. You answer "ok" or adjust it, and nothing is written until you do. There is no minimum page size: a group may be a single short page or section. A new concept goes to the page of its own subject, not of the block's: when it can be defined without mentioning the page's subject, the plan splits the block, putting the definition on the page where it belongs and leaving a `[[Page#Concept]]` link where the block used it. Each concept has its own heading on its owner page.
3. **Order.** The plan gives each block its place in didactic order, with what a block builds on before it: in a new page, the order of its sections; in an existing page, the exact point, inside a section or between two, such as "after the section X". Groups run from the newest to the oldest, a group counting as its newest block. Inside a group the blocks run in the plan's didactic order.
4. For each block, Claude writes the proposal directly into the files at the place the plan gives it, not at the end of the page by default: a section of an existing page or a new page, in your wording, with links and index changes. The text already on the page is never moved or rewritten; the block is only added. A block that continues the subject of a page created or edited in the same session becomes a section of that page, not a new page, unless the plan says otherwise. Where a new page goes depends on `new_page_placement`: with `folders`, the folder list in the context (two levels, without hidden or excluded folders, with page counts) lets Claude pick the best-fitting folder without reading the vault, and the plan states the choice in one line; you can answer "move to X". With `flat`, it goes in the new-pages folder. Nothing is committed yet.
5. The script shows the git diff. Your approval becomes a commit (`integrate: <block title>`) that also removes the block from the inbox. A change request makes Claude revise and show a new diff; skipping discards the edits through git and leaves the block in the inbox, and the next block of its group takes over the page or section the skipped block was to create; stopping ends the round.
6. At the end, one numbered list of the concepts touched, each with a suggestion and a half-line reason:
   - new concepts: review, or index only;
   - concepts that were only in the index: promote, or not;
   - concepts already scheduled that received new content are updated automatically.
7. Your answer is applied in one last commit (`integrate: queue N concepts`), and the session closes.

Claude suggests review for concepts that are core to what you study now, foundations for other concepts, subtle distinctions, or things the block got wrong. It suggests index only for author or book details, names and dates, things you can look up in seconds, and peripheral facts. When the queue is long (more than 35 concepts by default), it suggests fewer.

## Concept states and scheduling

A scheduled concept stores one date: the study day from which it is due. It stays due until it is asked. Positions in a list are never stored; the "top of the list" is just the earliest date.

A concept is **new** (queued, never asked), **learning** (failed before its first pass), in **review** (graduated, with an interval) or **relearning** (failed after graduating, back the next day). How intervals grow and shrink depends on the scheduling algorithm: `anki`, the default, is Anki's classic scheduler with Anki's default options; `thirds` is Recall's own, with no ease and a lapse that keeps a third of the interval. [scheduling.md](scheduling.md) gives both exactly, with their parameters and how to add another.

**Study day.** The day starts at 4 a.m. by default, so a session after midnight counts as the previous day. A concept that enters the queue becomes eligible on the next study day, so it is never asked on the day you integrated it.

**Within a session.** A failed concept is asked again at the end of the session, on the idea the answer missed, in different wording. That repeat isn't recorded; the script refuses a second answer for the same concept on the same day. The one way to change an answer is `srs.py amend`, for a verdict you contest (see below).

**Gaps.** What fails is usually one idea of a concept, not the whole concept. A fail opens a gap: one line, in the concept's record, naming the idea the answer missed. While it is open, the concept's questions test that idea instead of taking a new angle. A pass closes it when at least 3 days have passed since the previous answer, and the field disappears; a fail keeps it open and counts.

- **The schedule is untouched.** A failed concept already comes back the next day and on growing intervals, so those dates are the gap's dates. A gap changes what is asked, never when, and adds no question of its own. There is one question per concept per day, and at most one gap per concept.
- **It expires.** A wrongly named idea costs a few questions and goes away, and nothing accumulates. The page stays the only source: Claude reads it before asking, and closes a gap the page no longer supports.
- **A question on new content doesn't test the gap.** With a focus, the question targets the focus and the gap stays open. If that answer fails on another idea, its gap replaces the old one.
- **Gaps that won't close.** With 3 fails or more, `/maintenance` lists the gap and suggests rewriting that part of the page or making the idea a concept of its own. That is the only way a gap becomes permanent, and it takes your approval. It stays out of the quiz, so it doesn't interrupt.
- **Why not split concepts into scheduled ideas?** Each idea would need its own dates, text and upkeep whenever a page changes, and the daily limit would then buy a third as many concepts at about three ideas each. Gaps cost a smaller share of new concepts, because failed ideas are asked and failed more often, but the cost is only where you actually failed. The price is coverage: an idea that is never asked is never reviewed, as before.

**New content.** When `/integrate` adds content to a concept in review, the concept is put on hold: due the next study day, with a focus line describing the new part. The next question targets the focus; a pass keeps the interval as it was, and a fail is a normal lapse for the algorithm. A concept already in learning or relearning only gets the focus.

**Lapses and leeches.** A fail after graduation is a lapse. At the algorithm's leech threshold (8 lapses for `anki`, 10 for `thirds`), the concept is reported as a leech by `/quiz` and `/maintenance`: it is probably too broad (split it) or its page is unclear (rewrite it).

**Calibration.** `srs.py stats` reports the pass rate on answers given in the review phase over the last 30 days, split into young reviews (interval before the answer under `scheduling.mature_interval`, 21 days by default) and mature reviews (that interval or more), each with its count, plus the combined rate. A group with fewer than 30 reviews shows "not enough data". Calibrate on mature retention once it has enough reviews, and on young retention until then. Aim for 85 to 90%. Above 95%, reviews come too early: raise the algorithm's growth parameter (`interval_modifier` for `anki`, `multiplier` for `thirds`). Below 80%, lower it.

### The daily list

`srs.py due` builds the list; the `/quiz` command receives it ready.

- Up to 20 concepts per study day, counting what was already answered that day. Running `/quiz` again the same day continues where you stopped.
- Order: due learning, relearning and updated concepts, by date; then due reviews, most overdue first (ties: shorter interval first); then new concepts.
- New concepts: at most 5 per day, and only in slots left over. Concepts from `/integrate` come before concepts from `/promote`, in the order they were queued.
- When the reviews alone exceed the limit, the rest carry over to the next day and no new concept starts.

In practice, 20 concepts a day sustain about 2 to 3 new concepts per day once the schedule fills up, because each concept is reviewed about 7 times in its first year. A session of 20 open questions takes about 20 to 30 minutes.

## Log

`.srs/log.csv` has one line per recorded answer: study day, concept, pass or fail, and the phase and interval before the answer. Logs from before 0.5.0 get the interval column on the next answer; their older lines have no interval and count only in the combined rate. About 50 bytes per line, or a few hundred kilobytes per year.

The schedule doesn't depend on the log: the rules use only each concept's current state, which grows with the number of concepts, not with time. The log serves calibration (`srs.py stats`) and a possible future switch to FSRS, and it is the only record that can't be rebuilt later. Only the script reads it; it never enters the model's context. `srs.py amend` rewrites the result on today's line for a contested answer; no other part of the line or of the log changes. Question texts are not logged; each concept keeps its last three questions in the state, so the next question can take another angle.

## /quiz

1. The script opens a session and the command receives today's list, with each concept's page, focus and recent questions. Opening the session also turns off suggested replies in the input line, because they can show the answer (see Git, "Suggestions").
2. For each concept, Claude reads the owner page and asks one question, numbered against the day's total: the concepts already answered today plus those still to ask, which the daily limit caps. On a day with 15 questions the count ends at 15/15, and with 31 concepts due it ends at 20/20.
3. You answer. Claude says pass or fail with one line of justification, the expected answer and the page. You may contest, and your word wins: Claude runs `srs.py amend` with the result you stand for.
4. Each answer is recorded with `srs.py record`, which updates the state and the log.
5. Each failed concept opens a gap naming the idea the answer missed (see Gaps), and is asked again at the end on that idea, without recording. A concept whose verdict you contested leaves this repeat if it is amended to pass, and joins it if it is amended to fail.
6. At the end: the score, leeches, thin pages and misplaced concepts, and a commit such as `quiz: 17/20`.

Question rules, editable in `.srs/quiz-rules.md`:

- One question per concept, atomic, with a direct answer and no calculation.
- It must make sense years later: no author, book or course trivia; definitions that hold beyond the current topic.
- Asked from the owner page, not from the model's general knowledge. A page too thin for a good question is flagged with `srs.py flag` and reported by `/maintenance`. A concept defined on a page of another subject is flagged too (`srs.py flag "<page>" --misplaced "<Concept>"`), only after you agree, and `/maintenance` lists it with where it belongs; it never moves anything on its own.
- When an answer is right about the concept but wrong only on a prerequisite that has its own concept, the concept is judged by its own idea and the quiz offers to open a gap on the prerequisite instead.
- A different angle from the recent questions; aimed at the focus or the gap when there is one.
- Free recall, never multiple choice: recalling trains memory more than recognizing.
- Meant to consolidate, not to catch you out.
- Judged strictly: the core idea present and no conceptual error. Lenient grading stretches intervals for nothing.

## Existing material and /promote

`/setup` puts all existing material in the index only. It reaches the schedule gradually, always within the daily limit for new concepts and after the concepts from `/integrate`:

1. **Through `/integrate`**, when new study touches an old concept: the suggestion to promote it appears in the review list.
2. **`/promote <page, topic or concept>`** lists the matching concepts that are only in the index, with the same review-or-not suggestion.
3. **`/promote` alone** suggests a batch from the pages most recently touched by `/integrate`, found through the git log.

Promoting many concepts at once doesn't overload any day: the queue absorbs them, and the pace is the daily limit for new concepts. Raise `new_per_day` when you have more time.

Diary-style files (entries by date rather than by concept) are indexed like any other page. When there is time, `/integrate <file>` treats such a file like an inbox and moves its content into concept pages.

## /maintenance

1. `srs.py check` runs the deterministic checks on indexed pages: pages over the size limit (6,000 characters by default), concept names used on more than one page, descriptions over the limit, index links that don't resolve, index lines that can't be parsed, orphans (scheduled concepts missing from the index), pages that aren't in the index, pages without a valid seal, stale seals (files that are gone or are no longer pages), pages flagged as thin, and leeches.
2. A Haiku subagent with read-only tools audits up to 20 unsealed pages per run. For each page it judges: (A) every listed concept is explained on the page; (B) the page doesn't explain an important concept missing from its entry and not owned elsewhere; (C) none of its concepts duplicates another page's concept under a different name.
3. Pages that pass get a seal from `srs.py verify`, which refuses pages over the size limit or with names used elsewhere. Before the audit, `srs.py prune-seals` removes the stale seals, always and without asking, inside the session. The count goes in the commit message and the report.
4. The report lists failures with reasons, leeches, thin pages and unindexed pages.
5. Nothing else is fixed automatically. Splitting a page, merging concepts or renaming goes through the main agent, as an edit you approve by diff.
6. When the batch is empty and no seal was removed, the session ends without a commit. `srs.py commit` with nothing to commit prints "Nothing to commit." and exits 0.

**Seals are hashes.** A seal stores the git blob hash of the page and a hash of its index entry. Any change to either, through Recall or by hand in Obsidian, breaks the seal without anyone having to remember to remove it. The global duplicate check (criterion C) can still skip sealed pages safely: every new duplicate enters through a changed page, which has already lost its seal. When a duplicate is found, both pages stay unsealed until it is fixed. A seal outlives its page when the page is deleted, renamed, moved or excluded; `srs.py check` reports those entries and `srs.py prune-seals` removes them.

Leeches are where the review and the vault meet: a concept that keeps failing points to a page to split or rewrite.

## /setup

On a vault without Recall, `/setup` asks for the conversation language, the concept language, the paths, where new pages go (`folders` or `flat`), the heading level of inbox blocks, the exclusions, suggesting folders it detects (templates, daily notes, Excalidraw drawings), and the scheduling algorithm. It then runs `srs.py init`, which creates the git repository if needed (after asking), commits the vault's current content, writes `.srs/`, creates the inbox and the index if missing, completes `.gitignore`, and commits.

Each later run indexes one batch of pages (20 by default) with a Haiku subagent that only reads. The main agent merges the drafted entries into the index, resolving duplicates by choosing one owner (preferring a page about the concept over a diary-style page), and you approve the batch by diff. Nothing enters the schedule during setup, and every page starts unsealed. `/maintenance` reports pages created later by hand, and `/setup` indexes them.

## Git

Git is the review mechanism, the history and the safety net.

- **Review by diff.** During a command, Claude writes its proposal directly into the files. `srs.py diff` shows the change, and only your approval turns it into a commit; rejecting restores the files through git. When the session changed the index, `srs.py diff` first runs the index checks of `srs.py check` on the lines added or altered since HEAD (description over the limit, repeated concept name against the whole index, link that doesn't resolve, line not understood) and prints the warnings before the diff. They are warnings, not refusals: the commit stays allowed, and the commands tell Claude to fix them before asking for approval.
- **One commit per operation.** Content, index and state go in together, with a prefix: `integrate:`, `quiz:`, `promote:`, `setup:` or `maintenance:`. `/config` is the exception: `srs.py config` commits only the config file, as `config:`, with no session or diff, because the request already states the change and setting the old number back undoes it.
- **Your manual edits.** What you change in Obsidian is committed as `vault: manual edits` at the start of the next command, so each Recall commit holds only what that command did.
- **Sessions.** `.srs/session.json`, which is never committed, records the command in progress and every file written during it: by Claude (through the hook) and by the script. A commit includes only those files. Edits you make in parallel wait for the next `vault: manual edits`.
- **Interrupted sessions.** The next command shows what was left uncommitted and asks whether to keep it (commit) or discard it (`srs.py abort`). Discarding only touches the files the session wrote; files you created yourself are left alone. Folders left empty by deleting session-created files are removed too.
- **Suggestions.** `srs.py begin quiz` sets `promptSuggestionEnabled` to false in the vault's `.claude/settings.local.json` and saves the previous state in the session. `srs.py end` and `srs.py abort` restore it, so an interrupted quiz is restored when it is recovered. There is no option to keep suggestions on, and the file is left out of Recall's commits. The local file was chosen so other projects aren't affected. An environment variable that keeps suggestions on can't be changed from the script, so `begin` prints a note for the user instead.
- **Undo.** `git revert HEAD` undoes the last operation, including its effect on the schedule.
- **.gitignore.** `/setup` adds `.srs/session.json`, `.obsidian/workspace*.json` and `.trash/`.
- **Subfolders.** The vault may be a subfolder of a larger repository; Recall reads and commits only inside it.

## The hooks

Two hooks are registered in `hooks/hooks.json`.

**The guard.** `hooks/guard.py` runs before every Edit, Write and Bash call:

- It denies direct edits to `.srs/`.
- While a session is open, it allows edits to Markdown notes outside hidden folders without a permission prompt, and records each file in the session. The git diff is the review, so this behaves the same in every permission mode.
- It allows Bash to run the plugin's own `srs.py` without a prompt, but only when the command is exactly that script, with no shell operators, redirections or substitutions.
- In any other case it gives no opinion, and in folders where Recall isn't set up it does nothing.

**The welcome message.** A `SessionStart` hook with the matcher `startup` runs `srs.py welcome`, so it fires on a new session and not on resume, clear or compact. The command prints JSON with a `systemMessage`, which is shown to you directly: the model is not involved, so it costs no tokens. It took about 0.4 seconds on a vault of about 150 pages on WSL's `/mnt/c`, nearly all of it spent checking seals.

- Lines, showing only what applies: an interrupted session first, detected by `.srs/session.json` existing, with the advice to run a Recall command; then the day's status; then `/quiz` if there is something to ask; then `/integrate` if the inbox has blocks, with the first three titles, newest first as `/integrate` takes them, and `+N` for the rest; then `/maintenance` if it is worth running.
- Any edit breaks a page's seal, so a `/maintenance` line for every unsealed page would soon go unread. It appears with 5 or more indexed pages without a valid seal, with any such page 14 days after the last check, or with a gap that won't close or a leech. It tells what changed rather than only when the last run was, because a date alone doesn't say whether there is anything to do. The last check is the newest seal: the git log can't tell, since other commits use the `maintenance:` prefix. Comparing file dates first saved almost nothing, so every indexed page is hashed. If this part fails, the rest of the message is still shown.
- The day's status comes from `daily_list` and the log. With questions to ask and no answer today, it gives the total, split into reviews (learning, relearning, updated and review concepts) and new concepts, leaving out a zero. After some answers it gives what is left. With nothing left to ask, it gives the day's score, or without answers today, when the next quiz is, from `next_due`, which runs the real daily list for the coming days. When the pace lets nothing through, such as a queue of new concepts with `new_per_day` at 0, it says so instead of promising a date.
- It reads the vault's files and nothing else: no writes, and no git, which is slow on some filesystems.
- It can't break a session start. Any exception means no output and exit code 0, and so does a folder where Recall isn't set up. The folder comes from `cwd` in the hook's input, then `CLAUDE_PROJECT_DIR`, then the current directory.
- `language` is free text, so a value that contains "portugu" or starts with "pt", in any case, gets Portuguese, and any other value gets English. These are the only strings Recall ships in a language other than English, because no model is there to translate them.

## Portability

- **Plugin, not copied files.** Skills, script, rules, agents and hooks live in the plugin; the vault holds only configuration, state and content. Installing at project scope keeps commands and hooks out of your other projects.
- **Roles, not names.** The inbox, the index, the new-pages folder, the new-page placement and the inbox heading level are configuration. `/setup` warns about existing files or folders with similar names, such as an `Inbox/` folder.
- **Exclusions.** Recall always ignores the inbox, the index, `CLAUDE.md` and `AGENTS.md` files, and hidden folders. The `exclude` list adds patterns: `folder/` for a folder, `*.excalidraw.md` for file names anywhere, `Daily/*.md` for paths.
- **Preferences are editable defaults.** The conversation language, the concept language and the quiz rules.
- **English everywhere it's published.** Skills, script, commit messages and documentation are in English; Claude talks to you in the configured language. The welcome message's Portuguese strings are the one exception (see The hooks).
- **Requirements.** Claude Code, git and Python 3.8 or later, standard library only. Targets macOS, Linux and WSL; native Windows is untested.

## To revisit

- **The diff mechanism.** The git diff is version one; if `/integrate` feels heavy, it can change.
- **Algorithm parameters, limits and leech thresholds.** Calibrate with `srs.py stats` after a month of use.
- **Gaps.** After a month of use, check the pass rate on gap questions and how many gaps won't close, and adjust the 3 days and the 3 fails. If failed ideas keep being ones never asked before, reconsider how much coverage matters.
- **FSRS.** Compare it with the built-in algorithms after a few months, using the log.
- **Native Windows.** Test the script and the hook outside WSL.
