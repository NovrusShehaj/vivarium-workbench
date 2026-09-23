# The AI coding assistant (optional extension)

The workbench core is AI-free (`tests/test_no_ai_deps.py`). The assistant is a
separate, **opt-in** package, `vivarium_workbench_assistant/`. It plugs into the
core through the generic extension seam (`vivarium_workbench/lib/extensions.py`).
Nothing in `vivarium_workbench/` imports it; the import-linter contract "the
workbench core never imports the assistant extension" enforces that in CI. See
[ADR-0001](adr/0001-ai-free-core-optional-assistant-extension.md) for the
decision. That decision covers this fork only; it has not been accepted upstream.

It is a right-side panel. You chat with a model you choose (Bring Your Own Key),
give it context from the open workspace, and, in **Agent** mode, let it read
files, propose edits for you to review, and run a few checks with your approval.

## Install and enable

```bash
# in the workspace's venv (where the workbench itself is installed)
uv pip install 'vivarium-workbench[assistant]'          # httpx + keyring
uv pip install 'vivarium-workbench[assistant-google]'   # + google-auth, for Vertex AI

vivarium-workbench serve --workspace . --enable-extension assistant
# or: VIVARIUM_WORKBENCH_EXTENSIONS=assistant vivarium-workbench serve --workspace .
```

Without `--enable-extension`, the assistant does not load and adds no routes or
UI. It is skipped automatically in the read-only published bundle
(`vivarium-workbench-publish`) and in read-only servers. If the extras are
missing, the server logs an install hint and starts without it.

Open the panel with the chat icon at the bottom of the left rail or with
**Ctrl/Cmd + Shift + .** (configurable). Configure providers under
**Settings → AI Assistant**.

## Providers

The browser never calls a model provider. It talks only to the workbench
(`/api/ext/assistant/*`), and the **workbench server** makes every provider
request over `httpx`. No vendor SDKs are used
(`tests/assistant/test_assistant_no_vendor_sdks.py`).

| Provider | Wire format | Credentials (source) | Notes |
|---|---|---|---|
| Anthropic | Messages API (SSE) | OS keychain, env var, session | Context window and output limits come from model discovery |
| OpenAI | Chat Completions (SSE) | OS keychain, env var, session | |
| Google Vertex AI | OpenAI-compatible endpoint | ADC, service-account key file (path only), session access token | Needs `[assistant-google]`. Global or regional endpoint from project + location |
| Google AI Studio (Gemini API) | OpenAI-compatible endpoint | OS keychain, env var, session | Limits from the native model list |
| OpenRouter | Chat Completions | OS keychain, env var, session | Attribution headers are off unless you opt in |
| Local / OpenAI-compatible | Chat Completions | none, OS keychain, env var, session | Presets: Ollama (`http://127.0.0.1:11434/v1`), LM Studio (`http://localhost:1234/v1`), custom |

Each provider card offers **Test connection**, **Discover models**, per-model
capability overrides (for example "supports tools", which Agent mode needs),
and removal.

## Credentials

- **Where a key can live:**
  - The OS keychain, via `keyring` (service `vivarium-workbench-assistant`).
  - The server process's memory for this session only.
  - The environment variable you name. Only the *name* is saved; the value is read when needed.
  - For Vertex, a key file path (only the path is saved).
- **Where a key never goes:** browser storage, cookies or URLs; `workspace.yaml`
  or any workspace file; the HTML; logs; conversation transcripts; the audit log.
- **The credential endpoint is write-only.** No endpoint returns a stored key.
  Settings shows only whether a key is configured and its source, plus the
  last four characters for keys of at least 16 characters.
- The key field is cleared as soon as it is submitted.
- **Remove key** deletes the keychain entry.
- Known secrets are redacted from provider error messages, logs, audit records
  and streamed model text.
- If no usable keychain exists (for example on a headless Linux box), a key
  can still be kept for the server session. Settings says so.

## Local models (Ollama, LM Studio, llama.cpp server, vLLM, …)

Because the server makes the call, browser CORS and mixed-content rules do not
apply and `OLLAMA_ORIGINS` does not need to change. The workbench server must
be able to reach the URL.

- **Private and loopback addresses** are allowed only for the local endpoint
  you configured. Plain `http://` is accepted only for such endpoints.
- **A custom CA bundle** can be set per provider, in local mode only.
- **A slow first token** (a model still loading) shows "Loading model…". The
  first-token timeout is configurable per provider (5–900 s).
- **Test connection** reports a clear diagnosis for each common failure:
  connection refused, DNS failure, TLS mismatch, a 404 on `/models` (wrong base
  URL), an empty model list (pull or load a model), an unknown model, and
  malformed stream data (`tests/assistant/test_assistant_local_diagnostics.py`).

## Context

- **Automatic context.**
  - *Off.*
  - *Page summary* (ids only; the default).
  - *Page + the open study* (its `study.yaml` and status).
- **The context tray.** Add a study, investigation, composite, run-log tail,
  git diff, workspace manifest, file or search result. **Preview** shows exactly
  what will be sent, and calls no provider.
- **Every message stores a manifest of what it used, never a copy.** The chips
  under a message open the referenced item.
- **Safety filters, always on:**
  - A shared denylist, also used by the static route
    (`vivarium_workbench/lib/sensitive_paths.py`): `.git`, `.env*`, private keys,
    credential files, `.pbg/server`, and so on.
  - Realpath containment, including symlink escapes.
  - Binary files and large data artifacts are excluded.
  - Secrets are scanned for and redacted.
- **`.gitignore`d and `.vwbignore`d files** are excluded from automatic context
  and search.
- **Workspace content is framed as untrusted data** (`untrusted="true"`). The
  system prompt tells the model to treat it as data, never as instructions
  (`tests/assistant/test_assistant_injection.py`).
- **The first send to a cloud provider** asks for confirmation. This is on by
  default and can be turned off in Settings.

## Agent mode, proposals and approvals

Agent mode needs a model known to support tool calls.

| Tool category | Default | Examples |
|---|---|---|
| read | automatic | `read_file`, `list_dir`, `search`, study/investigation/run readers |
| propose | automatic (writes nothing) | `propose_edit`, `propose_create`, `propose_delete` |
| execute | **ask** every time | workspace lint, a pytest selector, a composite smoke run via the detached runner |
| shell | **disabled** (local mode only when enabled) | free-form command, with a per-invocation approval |

- **Approvals.** An approval is bound to a hash of the exact arguments and
  expires with the run.
- **Commands.** They run with a sanitized environment and in their own process
  group, so cancelling kills the whole tree.
- **Limits per run.**
  - 25 tool calls.
  - 3 identical calls (loop detection).
  - 200 000 tokens.
  - A wall clock of 10 minutes for chat and 20 minutes for agent runs.
- **Proposals.** A run's proposed changes are collected into one review card.
  It shows `+`/`−` diffs, validation (YAML, workspace JSON schemas, JSON, TOML,
  Python syntax) and a warning when a change touches code that runs with
  simulations.
- **Nothing is written until you apply.**
  - Apply checks the base hash first, so a stale diff is refused, and writes atomically.
  - **Apply & commit** commits only those files (`git commit --only`) with
    `Assisted-by` trailers. A path that already had your own uncommitted
    changes is left out of the commit.
  - **Undo** restores the snapshots. **Revert commit** reverts the commit.
- **Paths it cannot touch:** `.git`, `.pbg`, `reports/`, env/credential files
  and symlinks, whatever a proposal says.

## Storage

Paths use the per-user directories. Override them with
`VIVARIUM_WORKBENCH_CONFIG_DIR` and `VIVARIUM_WORKBENCH_DATA_DIR`; otherwise
`$XDG_CONFIG_HOME/vivarium-workbench` and `$XDG_DATA_HOME/vivarium-workbench`,
or `~/.config/…` and `~/.local/share/…`.

| File | What |
|---|---|
| `<config>/assistant/config.json` (`0600`) | Provider instances (no secrets) and preferences |
| `<config>/assistant/models.overrides.json` | Per-model context window and capability overrides |
| `<data>/assistant/workspaces/<sha256(realpath)[:16]>/…` (`0700`/`0600`) | Conversations as JSONL, per workspace (session-scoped in hosted mode) |
| `<data>/assistant/audit.jsonl` (`0600`) | Tool calls, approvals, edits, commits, credential changes. Redacted, never keys |
| `<data>/assistant/models.cache.json` | Model discovery cache (24 h) |

Conversation history can be turned off (memory only), given a retention period
in days, or deleted in one step (you confirm by typing the workspace name).

## Hosted and shared deployments

The mode comes from how the server was started:

- **local** is a loopback bind with no base path and no trusted proxy.
- **Everything else is hosted.** This includes an unknown bind address.
- **Pinning.** `VIVARIUM_WORKBENCH_ASSISTANT_MODE=local|hosted|disabled` pins the mode.

Hosted deployments have no per-user identity, so the assistant is **disabled**
unless the operator's deploy config (`VIVARIUM_WORKBENCH_DEPLOY_CONFIG`) turns
it on:

```yaml
assistant:
  enabled: true
  providers_allowed: [anthropic]
  credential_env: {anthropic: ANTHROPIC_API_KEY}   # operator-held keys only
  models_allowed: {anthropic: [claude-sonnet-5]}
  base_url_allowlist: []                            # exact URLs, for gateways
  allow_user_credentials: false                     # default
  persist_conversations: false                      # default: memory only
  tools: {execute: disabled, apply: disabled}       # "ask" to allow, per operator
```

When enabled, every request needs a browser session. Runs, conversations,
approvals and proposals are scoped to that session.

## Server hardening shipped with this work

These protect every workbench, with or without the assistant:

- **Host allowlist.** It defends against DNS rebinding. The default allows
  loopback names only; add a name with `--allowed-host` or
  `VIVARIUM_WORKBENCH_ALLOWED_HOSTS`.
- **CSRF/Origin checks** cover every unsafe method, including `PUT`, `PATCH`
  and `DELETE`.
- **The catch-all static route** never serves sensitive files or follows
  symlinks out of the workspace.
- **The outbound HTTP policy** applies to every provider request:
  - Every DNS answer is checked, and the connection is pinned to the checked address.
  - Metadata and link-local addresses are always blocked.
  - Private addresses are allowed only for the configured local endpoint.
  - Redirects are not followed, and URLs carrying credentials are refused.
  - Proxy variables are ignored.
  - JSON and SSE response sizes are capped.

## Privacy

Messages, the context you attach and tool results go to the provider you
selected, and only to it. The provider handles data under its own terms. Each
provider card links to them:

- [Anthropic](https://privacy.anthropic.com/)
- [OpenAI](https://openai.com/enterprise-privacy/)
- [Vertex AI](https://cloud.google.com/vertex-ai/generative-ai/docs/data-governance)
- [Gemini API](https://ai.google.dev/gemini-api/terms)
- [OpenRouter](https://openrouter.ai/privacy). OpenRouter routes to several
  upstream providers.

These links were not re-verified when this document was written.

## Troubleshooting

| Symptom | Fix |
|---|---|
| No chat icon in the rail | Start with `--enable-extension assistant`, and install `[assistant]` in the venv the server runs from |
| "Disabled on shared deployments" | The server is not local (non-loopback bind, base path, or trusted proxy). Run it locally or enable hosted mode (above) |
| "No usable OS keychain" | Use an environment variable or a session-only key |
| Test connection: connection refused / DNS / TLS | Check the base URL, that the server is running, and the scheme (`http` vs `https`) |
| "could not find that model or endpoint (404)" | Wrong base URL (usually a missing `/v1`), or a model that is not pulled or loaded |
| Agent toggle refuses a model | Mark the model as tool-capable under the provider's model list if you know it supports tools |
| 400 `host not allowed` | You reached the server by a name that is not allowlisted. Add it with `--allowed-host` |

## Verification status

Automated tests use a scriptable fake provider server on `127.0.0.1`
(`tests/assistant/fake_llm.py`). Every provider type runs over real sockets
through the real adapter, codec and outbound client, and the browser E2E suite
(`tests/e2e/test_e2e_assistant.py`) drives the panel against it. No automated
test calls a live provider.

**No live-provider smoke test has been performed for this implementation.** No
provider credentials were used. Before relying on a provider, run this checklist
against the real service:

1. Add the provider in Settings, run Test connection, and discover models.
2. Stream a short answer. Stop mid-stream, then Retry and Regenerate.
3. Run Agent mode with a tool-capable model: read a file, then propose and apply an edit.
4. Remove the key and confirm the keychain entry is gone.
