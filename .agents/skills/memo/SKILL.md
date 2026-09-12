---
name: memo
description: Save the current task state to persistent project memory, promote reusable lessons into repository skills, and trim stale or duplicated memory. Use when the user invokes $memo or asks to preserve the current session for future work.
---

# Memo

Capture the useful state of the current work, promote durable guidance, and leave project memory concise. Complete the workflow rather than only describing what should be saved.

## Save current state

Use the active agent's supported persistent project-memory mechanism and follow its update rules. Do not assume that memory has a particular path or that generated memory indexes may be edited directly.

Record a concise snapshot containing:

- what changed and the area of the repository involved;
- whether the work is complete, blocked, or in progress;
- decisions, outcomes, and follow-up state worth carrying across sessions.

Do not duplicate guidance already present in repository instructions or skills.

## Promote reusable lessons

Review existing project memory for reusable conventions, foot-guns, commands, and debugging recipes. Move each durable lesson into the most relevant repository skill, creating a focused skill under `.agents/skills/<name>/SKILL.md` only when no existing skill fits.

Keep promoted guidance independent of a particular coding agent. Remove or supersede the corresponding memory item using the active memory mechanism once the skill is authoritative.

## Trim memory

Remove or supersede items that are already captured elsewhere, specific to a finished task, stale, or contradicted by newer information. Keep the remaining project memory concise, ideally no more than about 30 lines.

Report what was saved, promoted, and trimmed. Do not commit the resulting changes unless the user separately asks for a commit.
