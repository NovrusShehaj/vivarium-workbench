# ADR-0001: AI-free core with an optional assistant extension

- **Status:** Accepted **for this fork only**. It has not been proposed to or
  accepted by upstream (vivarium-collective/vivarium-workbench).
- **Date:** 2026-09-23
- **Plan:** [docs/dark-mode-and-ai-coding-assistant-plan.md](../dark-mode-and-ai-coding-assistant-plan.md), Appendix A (ADR-01)

## Context

- **The workbench promises an AI-free server.** `docs/ai-onboarding.md` §4.1
  and `docs/REFACTOR-PLAN.md` §0 both say so, and `tests/test_no_ai_deps.py`
  fails the build if any module under `vivarium_workbench/` imports an LLM SDK.
- **The investigation report promises no model call and no invented prose.**
- **Users want an in-app coding assistant.** They want to Bring Their Own Key
  for Anthropic, OpenAI, Vertex AI, AI Studio, OpenRouter or a local model.

## Decision

- **Keep the core AI-free.**
  - `vivarium_workbench/` contains no AI code and no AI dependencies.
  - `tests/test_no_ai_deps.py` is unchanged: no ignore list, no weakened scan.
- **Ship the assistant as a separate package.**
  - The package is `vivarium_workbench_assistant/`, in the same wheel.
  - It installs with the `assistant` / `assistant-google` extras.
  - It is registered under the `vivarium_workbench.extensions` entry-point group.
  - It loads only when enabled explicitly (`--enable-extension assistant` or
    `VIVARIUM_WORKBENCH_EXTENSIONS=assistant`).
- **The core gains a generic, AI-agnostic extension seam**
  (`lib/extensions.py`):
  - routes under `/api/ext/<id>`, registered before the catch-all;
  - assets under `/ext/<id>/assets/`;
  - escaped UI slots, and a right-side panel host (`static/sidepanel.js`);
  - a Settings section registry (`static/settings.js`);
  - a `vivSuggest` provider hook.

  The seam skips extensions in read-only and snapshot modes.
- **Import-linter enforces the direction.** A `forbidden` contract ("the
  workbench core never imports the assistant extension") keeps
  `vivarium_workbench` from importing `vivarium_workbench_assistant`, directly or
  transitively. The extension may import the core's public seam and read APIs.
- **The extension avoids vendor SDKs.** It talks to providers over `httpx` with
  two wire codecs (`openai-chat`, `anthropic-messages`) and data-driven provider
  profiles. `tests/assistant/test_assistant_no_vendor_sdks.py` keeps it that way.

## Alternatives considered

1. **Put the assistant in `vivarium_workbench/lib/` and relax the test.**
   Rejected: it silently erodes a documented guardrail.
2. **Put it in the core but call providers over raw HTTP.** Rejected: it
   passes the letter of the test while defeating its intent.
3. **A separate repository or distribution** (like viva-superpowers). Viable,
   but slower to iterate. It remains the fallback if upstream wants no AI code
   in this wheel: the extension needs no core changes to move out.

## Consequences

- **A small seam API to maintain** (`WorkbenchExtension`, `ExtensionContext`,
  `ExtensionAssets`, `UiConfig.extensions`). It is useful to non-AI
  extensions too.
- **Two packages share one wheel.** `pyproject.toml` `[tool.hatch…] packages`
  lists both.
- **Security work landed in the core because it protects every user**, with or
  without the assistant (and is upstreamable on its own):
  - the Host allowlist against DNS rebinding;
  - CSRF on every unsafe method;
  - the static-route denylist;
  - symlink containment.
- **The docs now describe an "AI-free core; optional, opt-in assistant
  extension"** (`README.md`, `docs/ai-onboarding.md`, `docs/ARCHITECTURE.md`,
  `CLAUDE.md`, `AGENTS.md`).

## Revisit if

- Upstream rejects the extension. Move it to a separate distribution; the seam
  and hardening stay.
- Upstream decides AI belongs in the core.
