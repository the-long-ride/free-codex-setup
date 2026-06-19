# Free Claude Code

## Quick Start

`free-codex` runs the FCC server from this checkout only. It does not edit your Codex configuration automatically.

### 1. Start the FCC server

#### Windows PowerShell

Run the PATH setup script once from this repo:

```powershell
.\scripts\add-repo-scripts-to-path.ps1
```

Then start the local FCC server:

```powershell
free-codex
```

#### macOS / Linux

Start the local FCC server from this repo:

```bash
free-codex
```

### 2. Open the local Admin UI

Open the local Admin UI in your browser after the server starts. From there:

1. Copy the Codex `config.toml` snippet.
2. Download the Codex model catalog.
3. Paste the config into your Codex config file.
4. Put the downloaded model catalog in your Codex home, usually `~/.codex/codex-model-catalog.json`.

### 3. Choose one authentication approach

FCC supports two ways to authenticate Codex clients:

#### Option A: Use the env key from the FCC server

Use this when you want Codex to authenticate with the same FCC server key you already trust.

1. In the Admin UI, copy the Codex `config.toml` snippet for the FCC provider.
2. Make sure the `api_key` in that snippet matches your FCC server auth key.
3. Save the snippet in your Codex `config.toml`.

#### Option B: Use a normal API key value directly in `config.toml`

Use this when you want to paste a fixed key directly into the FCC provider block.

1. Copy the Codex `config.toml` snippet from the Admin UI.
2. Replace the `api_key` value with the API key you want Codex to send.
3. Save the snippet in your Codex `config.toml`.

### 4. Optional: store the key in an environment variable

If you prefer managing the FCC key as an environment variable on your machine, use `FREE_CODEX_KEY`.

#### Windows PowerShell

Set it once for your user account:

```powershell
[Environment]::SetEnvironmentVariable("FREE_CODEX_KEY", "<your fcc-server key>", "User")
```

Remove it later:

```powershell
[Environment]::SetEnvironmentVariable("FREE_CODEX_KEY", $null, "User")
```

#### macOS / Linux

Set it for the current shell:

```bash
export FREE_CODEX_KEY="<your fcc-server key>"
```

Remove it from the current shell:

```bash
unset FREE_CODEX_KEY
```

If you added it to a shell profile such as `~/.bashrc`, `~/.zshrc`, or `~/.profile`, remove that line there too.

### 5. Codex config example

Example FCC provider block for Codex:

```toml
model_provider = "fcc"
model_catalog_json = "~/.codex/codex-model-catalog.json"

[model_providers.fcc]
name = "Free Claude Code"
base_url = "http://127.0.0.1:8082/v1"
api_key = "your-fcc-server-key"
wire_api = "responses"
```


If you keep the key in `FREE_CODEX_KEY`, treat that variable as your local storage helper and paste the resolved key value into `env_key`. Codex does not expand `$FREE_CODEX_KEY` automatically inside `config.toml`.

```
[model_providers.fcc]
name = "Free Claude Code"
base_url = "http://127.0.0.1:8082/v1"
env_key = "FREE_CODEX_KEY"
wire_api = "responses"
```
