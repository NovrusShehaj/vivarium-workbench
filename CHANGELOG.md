# Changelog

## Unreleased

### Theme

- **The theme now follows the operating system by default.** It is a
  three-state preference: **System**, Light or Dark.
  - Users who never chose a theme get the OS appearance, and it updates live.
  - An explicit Light or Dark, including values saved by the old toggle, is
    still honoured.
  - To keep the old light default, choose **Light** once in Settings →
    Appearance.
  - Operators can revert the default by setting `D` in
    `templates/_theme_boot.html` and `DEFAULT_PREFERENCE` in `static/theme.js`
    to `'light'`. `tests/test_theme_boot.py` keeps the two equal.
- **New Settings page** (`#settings`) with an Appearance radio group. A rail
  menu (System / Light / Dark) replaces the old two-state toggle, and there is
  an optional Alt+Shift+T shortcut, off by default.
- **No flash of the wrong theme.** A tiny pre-paint boot sets the theme before
  first paint on every page, the study page and the loom viewer. A host cookie
  keeps the choice across server restarts on a new port.
- **Dark mode covers the whole UI.**
  - Design tokens are in `static/tokens.css`, and hard-coded colours were
    replaced across the stylesheets, templates and JS-built HTML.
  - The loom viewer (React Flow `colorMode`), Plotly charts, inline SVG charts,
    the investigation graph and the generated comparative viz documents all
    follow the theme.
- **Accessibility:**
  - WCAG AA contrast in both themes, checked by axe-core in CI.
  - Visible focus rings, and 3:1 form-control borders.
  - Gate dots with a shape per state.
  - Modal dialogs with focus trap, Escape and focus restore.
  - Dark-mode checkboxes and radios are visible again.
  - Reduced-motion and forced-colours support.

### Security (all users)

- **Host allowlist (DNS-rebinding defense).** Only loopback names are accepted
  on a loopback bind; add names with `--allowed-host` or
  `VIVARIUM_WORKBENCH_ALLOWED_HOSTS`.
- **CSRF/Origin checks** now cover every unsafe method, including `PUT`.
- **The static catch-all route** no longer serves sensitive files (`.git`,
  `.env*`, keys, credentials, `.pbg/server`) or follows symlinks out of the
  workspace.

### Optional AI coding assistant (fork)

- **A new opt-in extension, `vivarium_workbench_assistant`** (the `[assistant]`
  extra and `serve --enable-extension assistant`). It is a bring-your-own-key
  chat and coding panel for Anthropic, OpenAI, Google Vertex AI, Google AI
  Studio, OpenRouter and local OpenAI-compatible servers (Ollama, LM Studio).
- **The core stays AI-free.** See `docs/assistant.md` and
  `docs/adr/0001-ai-free-core-optional-assistant-extension.md`.
- **A generic extension seam in the core**: `/api/ext/<id>` routes, a
  right-side panel host, Settings sections, and a `vivSuggest` hook.
