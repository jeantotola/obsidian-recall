# Recall rules

These rules apply to every Recall command.

## Principles

1. **Approving must be cheap.** Propose, and let the user answer "ok". Discussion is the exception. Any inbox block may wait for another day. A system that is costly to maintain gets abandoned.
2. **The concept is the key.** Concept names link the index, the review schedule and the pages, so they are unique across the index and stable.
3. **The voice is the user's.** Move, link and adjust what the user wrote as little as possible. Put anything you add on your own in a callout that starts with `> [!claude]`. When the user's text has a factual error, keep it and point it out in the chat; never fix it silently. Never pad pages with textbook content.
4. **State and arithmetic belong to the script.** Never write dates or intervals yourself, and never edit files under `.srs/`; call `srs.py`. A hook blocks direct edits there.
5. **Integration ignores page size, and places pages by the configured placement.** Page size is /maintenance's job. With `new_page_placement` set to `folders` (the default), a new page goes in the existing folder whose topic fits best; when none fits, propose a new folder that follows the vault's naming convention, and use the new-pages folder only when the vault has no thematic folders. With `flat`, every new page goes in the new-pages folder and the vault's folders are ignored.
6. **Git-native.** Every operation ends in a commit made by `srs.py commit`. Content is committed only after the user approves the diff that `srs.py diff` shows. Never run git yourself: use `srs.py begin`, `diff`, `commit`, `discard`, `end` and `abort`.

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
- A page lists only the concepts it explains, not the ones it mentions. Other pages link to the owner page.
- Concept names are unique across the whole index. When a name is ambiguous, put the qualifier in the name itself, in parentheses.
- Granularity: a concept is something that would have its own entry in a glossary ("Eigenvectors", not "Linear algebra").
- Concepts marked `[review]` or `[queued]` are in the schedule. Never rename or remove one in the index without also running `srs.py rename` or `srs.py remove`.
