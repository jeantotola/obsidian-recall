# Recall rules

These rules apply to every Recall command.

## Principles

1. **Approving must be cheap.** Propose, and let the user answer "ok". Discussion is the exception. Any inbox block may wait for another day. A system that is costly to maintain gets abandoned.
2. **The concept is the key.** Concept names link the index, the review schedule and the pages, so they are unique across the index and stable.
3. **The wiki is the goal; the voice is the user's.** Each block should leave the vault better organized, not just longer. Before adding to a page, look at what it already says on the subject: if that text is thin, misplaced or made redundant by the block, propose moving, rewriting or merging it, as items of the plan the user approves. Rewrite the user's text only through such an item, and keep their wording wherever it survives. Put new ideas or informative text of your own in a callout that starts with `> [!claude]`. Text that only gives structure, such as a linking sentence, a transition or a heading, and states nothing the vault doesn't already say, needs no callout. An error in the user's text follows the Errors section: never fix it silently. Never pad pages with textbook content.
4. **State and arithmetic belong to the script.** Never write dates or intervals yourself, and never edit files under `.srs/`; call `srs.py`. A hook blocks direct edits there.
5. **Integration ignores page size, and places pages by the configured placement.** Page size is /maintenance's job. With `new_page_placement` set to `folders` (the default), a new page goes in the existing folder whose topic fits best, following the folder map when the vault has one; when none fits, propose a new folder that follows the vault's naming convention, and use the new-pages folder only when the vault has no thematic folders. With `flat`, every new page goes in the new-pages folder and the vault's folders are ignored.
6. **Git-native.** Every operation ends in a commit made by `srs.py commit`, except `srs.py config`, which commits its own change. Content is committed only after the user approves the diff that `srs.py diff` shows, with two exceptions. The corrections of an /integrate plan and the typos of /quiz, whose exact text before and after the user approved, are committed once you have checked that `srs.py diff` holds nothing else. And in /integrate with `integrate_review` set to `agent`, the user approves the plan, and a block is then committed with `srs.py commit --review` once the script's checks and the judge agents have passed it. A block that doesn't pass is parked for the user, never committed. If `srs.py diff` prints index warnings, fix them before asking for approval; they warn, they don't block the commit. Never run git yourself: use `srs.py begin`, `diff`, `commit`, `discard`, `end`, `abort`, `plan` and `review`.

## Errors

Every page a command makes you read is a chance to correct it. Look for what is wrong in it (in /quiz, only take note of what you happen to see): in the text, in its index descriptions, in your own callouts. Only the session's model does this, never a subagent, and a Haiku model skips it. What an auditor reports is a lead: open the page and confirm it before showing it to the user.

Show each error with the passage, why it is wrong in one line, and the exact text that would replace it in every place that repeats it. Nothing changes until the user decides, and the decision never interrupts what the command was doing: each command says when to ask. Typos go together in one item.

- **Wrong:** correct it.
- **Ambiguous:** a few words in the text itself settle it, with no callout. If they would need something the user hasn't learned, it is incomplete.
- **Incomplete**, only when what is missing makes the text misleading or leaves a concept impossible to ask about: a callout that asks and doesn't answer, so nothing the user hasn't learned enters the vault. Its title is in the user's language:

  ```text
  > [!question] Incomplete
  > Do two processes share the offset after a fork?
  ```

  No command asks about it again, and the quiz doesn't ask what it says is missing. When a block of /integrate answers it, propose using the block there and removing the callout.
- **Left as it is:** rare, so a "no" to an error, or an "ok" to a list, isn't enough. Ask about it alone, in a message of its own, showing the comment that would go next to the passage, such as `%% checked: deliberate simplification %%`, and write it only on an explicit yes. Without that yes, the error counts as not now. Don't raise a passage that has one.
- **Not now:** it stays pending, as each command says.

Before committing a correction, run `srs.py reseal "<page>" ...` on the pages it touched: they keep their seal without a new audit. When it changes the core idea of a concept in the schedule, run `srs.py update "<Concept>" --focus "<what changed>"`.

## The review list

/integrate, /maintenance and /promote ask which concepts go into the quiz. The choice is always the user's.

Show all the candidates first, in one numbered list. Then suggest, by number and with a half-line reason each, which go to review and which stay in the index only. Suggest review for concepts that are core to what the user is studying now, foundations for other concepts, subtle distinctions, or things a block got wrong. Suggest index only for author or book details, names and dates, things one looks up in seconds, and peripheral facts. If the queue line says the queue is long, suggest fewer and say why. The user answers "ok" or adjusts, for example "ok, without 3 and with 5".

## Notes in the inbox

A `{CLAUDE: ...}` inside an inbox block is a note from the user to /integrate: an instruction or a question, in free text, about the passage where it sits. The block list counts the notes of each block.

- A note is a request, not an approval: the plan still needs the user's ok. Point out where a note conflicts with the vault, and ask in the plan when it is ambiguous.
- A note never goes into a page, not even quoted: write the passage without it. A hook denies the write otherwise. Don't delete it from the inbox during the session, because that would change the id of its block; it leaves with the block.
- A note that asks for a correction of the user's text authorizes only the exact change the plan shows, as the Errors section says.

## Running srs.py

- Run it exactly as the command shows it, `python3 <plugin path>/scripts/srs.py <command> ...`, on its own: no `cd`, pipes, redirections or `&&`.
- Put concept names, paths and messages in double quotes.
- When it reports an error, read it: it says what to do next.

## Language

- Talk to the user in the configured language.
- Write concept names and index descriptions in the configured concept language.
- Keep the user's own wording in their notes, whatever language it is in.

## The index

The index lists, for each page, the concepts that page explains:

```text
## [[Page name]]
- Concept
- Ambiguous concept (qualifier) — short description
```

- One section per page. Write the link as Obsidian would: the file name, or the path from the vault root without `.md` when two files share that name.
- One concept per line. A description is optional: add it only when the name alone is ambiguous, after a space, an em dash and a space, within the configured character limit. It clarifies; it doesn't define.
- A page lists only the concepts it explains, not the ones it mentions. It explains a concept when someone could answer "what is X?" from the page alone; an example or a notation on its own doesn't. Other pages link to the owner page.
- Concept names are unique across the whole index. When a name is ambiguous, put the qualifier in the name itself, in parentheses.
- Granularity: a concept is something that would have its own entry in a glossary ("Eigenvectors", not "Linear algebra").
- Concepts marked `[review]` or `[queued]` are in the schedule. Never rename, merge or remove one in the index without also running `srs.py rename`, `srs.py merge` or `srs.py remove`.
