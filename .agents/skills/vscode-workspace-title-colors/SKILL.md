---
name: vscode-workspace-title-colors
description: Apply one plain-English colour to both active and inactive VS Code title-bar backgrounds in a .code-workspace file. Use when a user wants a workspace window to be visually identifiable by its title bar.
---

# VS Code Workspace Title Colors

Find `*.code-workspace` files in the current repository, excluding copies under `.agents/worktrees/`. If the user named a target, use the matching file. If there is one candidate, use it; if there are multiple plausible candidates, ask the user which file to change before editing.

Ask the user for one plain-English title-bar colour unless it was already supplied. Translate the name to a representative `#RRGGBB` value that VS Code accepts. Use the conventional value for well-known descriptive colours; ask for clarification only when genuinely different interpretations are plausible. Tell the user which hex value was chosen.

In the selected workspace file, merge these entries into `settings.workbench.colorCustomizations`:

```json
"titleBar.activeBackground": "<chosen colour>",
"titleBar.inactiveBackground": "<chosen colour>"
```

Use the exact same hex value for both entries. Preserve every unrelated folder, setting, colour customization, comment, and formatting choice. Create the containing objects only when absent. Edit only the selected workspace file, then inspect the resulting object and report the file, English colour, and hex value applied. Do not commit the change unless the user separately asks.
