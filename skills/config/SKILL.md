---
name: config
description: Show or change Recall's daily pace in this vault, how many new concepts and how many concepts in total the quiz asks per day, whether the quiz allows a second try after a wrong answer (retry), and who reviews /integrate (the user or judge agents). Use when the user wants more or fewer new concepts or reviews per day, to turn retry on or off, or to switch how /integrate is reviewed.
argument-hint: "[what to change]"
allowed-tools:
  - Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/srs.py *)
---

# Recall /config

!`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/srs.py context config`

The block above comes from Recall's script. Call the script as `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/srs.py <command>`, written `srs.py <command>` below, on its own on the command line. Follow the rules at the end of the block.

The user asked: $ARGUMENTS

If the block says NOT SET UP, tell the user to run /setup, and stop. An open session doesn't get in the way: this command needs none and commits only the config file.

1. Only asking what the pace is, or no request at all: show the current pace and stop.
2. Numbers given: run `srs.py config --new-per-day N --daily-limit N`, with only the flags they asked to change. Their request is the approval, so don't ask again. The daily limit counts reviews and new concepts together.
3. No numbers ("quero mais revisões"): propose numbers based on the block, in one line, and wait for "ok". If reviews already exceed the daily limit, raising new concepts alone does nothing, so propose raising the limit.
4. A change of who reviews /integrate ("human" to approve each diff yourself, "agent" to let judge agents check each block after you approve the plan): run `srs.py config --integrate-review human` or `--integrate-review agent`. The request is the approval. Say in one line what the mode means: with `agent`, the user still approves the plan and the final review list, and supervises the blocks the judges send back.
5. Retry ("a second try after a wrong answer"): run `srs.py config --retry on` or `--retry off`. The interval after a retry stays the same by default; a number of days sets it, never above the current interval: `srs.py config --retry-interval 3`, or `--retry-interval same` to go back. The request is the approval.
6. Close by relaying the script's lines on today's effect. A change made during a quiz applies from the next `srs.py due`.

Any other setting is edited by hand in `.srs/config.json`: say so, and point to the Configuration section of the README. Never edit that file yourself.
