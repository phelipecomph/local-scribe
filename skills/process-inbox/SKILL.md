---
name: process-inbox
description: >-
  Process the transcript notes that local-scribe drops into the vault inbox.
  Separates real signal from ambient background noise, summarizes the signal
  into the day's note, surfaces action items, and archives the processed
  fragments. Triggers: "process inbox", "process my transcripts",
  "/process-inbox".
---

# Process Inbox

You turn raw transcript notes (produced by local-scribe) into something useful,
then clear the inbox. The notes are Markdown files; many ambient captures are
**mostly background noise** (videos, music, games playing near the mic) with
only occasional real signal. Your job is to extract the signal and discard the
rest.

## Inputs

- **Inbox folder**: where local-scribe writes notes (its `$VAULT_PATH/$NOTES_SUBDIR`,
  usually `.../inbox/`). If you don't know it, ask once, then remember it.
- **Note shapes**:
  - `meeting-YYYY-MM-DD-HHMM.md` — a push-to-record session (usually real).
  - `ambient-YYYY-MM-DD-HHMM.md` — an always-on chunk (often noise). Tiny files
    are empty templates — skip them.
- **Destinations** (ask once if unknown): the daily-notes folder and an archive
  folder for processed fragments.

## Steps

1. **List** the transcript notes in the inbox. Group them by date. Skip empty /
   template files (very small).
2. **Read and classify** each note. For noisy ambient captures, keep only
   fragments that are real work or personal signal — decisions, names, numbers,
   tickets, commitments, plans. Treat media/game/TV audio as noise.
   - For large batches, work date-by-date to stay within limits; consider
     delegating per-date reads to sub-agents if your tool supports it.
3. **Summarize the signal** into the relevant **daily note** (create it if
   missing), grouped by topic/time. Be factual and concise — no narration.
4. **Surface action items**: collect concrete to-dos (who / what / when) into a
   short list at the end of your reply and, if the vault has one, append them to
   the task/backlog note.
5. **Archive**: move every processed fragment (signal or noise) into the archive
   folder so the inbox ends empty. Never delete — move.
6. **Report back**: how many files processed, which dates had real signal, and
   the action items found.

## Rules

- Write notes in the **same language as the source** (the vault's language) —
  do not translate the content.
- Faithfully report: if a day was all noise, say so; don't invent signal.
- Idempotent: running again on an empty inbox should do nothing.
- Don't touch files outside the inbox except the daily/task destinations.
