# Skills (optional)

local-scribe's job ends when the note lands in your vault. These **optional
agent skills** help you *consume* those notes day-to-day in the agent you
already use (e.g. Claude Code): turn a noisy inbox into daily notes and action
items, conversationally.

They are plain Markdown skills — nothing here is required to run local-scribe.

## What's here

| Skill | What it does |
|-------|--------------|
| [`process-inbox`](process-inbox/SKILL.md) | Batch-process the inbox: split signal from noise, summarize into daily notes, surface action items, archive the fragments. Pairs with local-scribe's optional end-of-day dispatch. |
| [`extract-actions`](extract-actions/SKILL.md) | Pull action items / decisions / open questions out of a single note into a clean list. |

## Install

Copy the skill folders into your agent's skills directory.

**Claude Code — user scope (all projects):**
```bash
cp -r skills/process-inbox skills/extract-actions ~/.claude/skills/
```

**Claude Code — project scope (one vault/repo):**
```bash
cp -r skills/process-inbox skills/extract-actions <your-project>/.claude/skills/
```

Then invoke them by name in conversation (e.g. "process inbox") or as a slash
command (e.g. `/process-inbox`), depending on your agent.

## Note on the end-of-day hook

local-scribe's daemon can optionally fire `process-inbox` in your vault at the
end of the day (a thin, disableable hook). Installing the `process-inbox` skill
above is what gives that hook something to run. If you don't want any automatic
dispatch, leave the hook off and run the skill manually whenever you like.
