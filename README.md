# Free Claude Code

## Quick Start

Windows PowerShell only.

1. Run the PATH setup script once from this repo:

```powershell
.\scripts\add-repo-scripts-to-path.ps1
```

2. Start the local FCC server and point Codex at it:

```powershell
free-codex
```

3. When you are done, restore the standard Codex config:

```powershell
free-codex --sleep
```

`free-codex` runs the FCC server from this checkout, updates the Codex config to use the local `fcc` provider, and restores the previous Codex settings when you stop it or run `--sleep`.
