---
name: extract-actions
description: >-
  Pull action items, decisions, and open questions out of a single transcript or
  summary note and turn them into a clean task list. Triggers: "extract actions",
  "what are the action items", "action items from this note",
  "/extract-actions".
---

# Extract Actions

Given one transcript/summary note (produced by local-scribe), extract the
actionable content and present it as a tight, deduplicated list.

## Input

- A note path (or the note the user just referenced / pasted). If ambiguous, ask
  which note.

## Steps

1. **Read** the note.
2. **Extract** three buckets:
   - **Action items** — concrete to-dos. Format each as `owner — what — when`
     (use "me"/unknown when the owner isn't stated; omit date if none).
   - **Decisions** — things that were decided (one line each).
   - **Open questions / follow-ups** — unresolved items needing a reply or input.
3. **Deduplicate and merge** near-identical items; keep the clearest wording.
4. **Output** the three buckets as short Markdown lists. If a bucket is empty,
   say so explicitly rather than inventing entries.
5. If the user asks, **append** the action items to their task/backlog note or
   create tickets — otherwise just return the list.

## Rules

- Keep the note's original language; don't translate.
- Only extract what's actually in the note — no speculation.
- Prefer fewer, high-signal items over an exhaustive dump.
