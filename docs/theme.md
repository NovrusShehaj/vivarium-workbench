# Theming: System / Light / Dark

The workbench has a three-state theme **preference** and a two-state
**resolved** theme:

| Preference (what the user chose) | Resolved (what is painted) |
|---|---|
| `system` — follow the OS, live (**the default**) | `light` or `dark` from `prefers-color-scheme` |
| `light` | `light` |
| `dark` | `dark` |

The browser owns the preference; it is a per-person, per-device display
setting. It is never written to `workspace.yaml` (which is committed and
shared) or to the server.

## How it works

- **Pre-paint boot.** `templates/_theme_boot.html` is a ~500-byte inline
  script. It is included first in `<head>` of every shell (`index.html.j2`,
  `study-detail.html`), injected into the loom viewer and its published copy,
  and inlined verbatim into generated standalone documents
  (`lib/theme_embed.py`). It resolves the theme before anything paints:
  1. `localStorage['viv.theme']`;
  2. the host cookie `viv_theme`, which carries the choice across server
     restarts on a new port;
  3. the default, `system`.

  It then writes `<html data-theme="light|dark" data-theme-pref="…">` and
  `style.colorScheme`.
- **Runtime.** `static/theme.js` exposes `window.vivTheme`:

  | Member | What it does |
  |---|---|
  | `getPreference`, `getResolved`, `setPreference` | Read or change the theme |
  | `subscribe` | Register a callback for theme changes |
  | `token` | Read a design token's current value |
  | `plotlyLayout`, `applyToPlotly` | Theme a Plotly figure |
  | `syncThemedImages` | Swap light/dark image variants |

  It follows OS changes while the preference is `system`. It persists choices
  to localStorage and the cookie (`SameSite=Lax`, `Secure` on HTTPS). It
  re-applies on `storage` events, so other tabs, the study iframe, the loom
  and pop-outs follow. It dispatches `viv:themechange` on `window`. The old
  globals `_setTheme` / `_toggleTheme` still work, and so do stored values
  from the old toggle.
- **Controls.**
  - **Settings → Appearance** is a radio group with a live "System (currently
    Dark)" note. It is the authoritative control.
  - **A rail menu button** (System / Light / Dark as `menuitemradio`) is always
    visible at low emphasis.
  - **An optional shortcut**, Alt+Shift+T. It is off by default and ignored
    while typing.
- **Tokens.** `static/tokens.css` is the only file that defines colours. It
  covers both themes: surfaces, text levels, borders (including
  `--border-control`, 3:1 for form controls), status fg/bg/border, syntax,
  diffs, charts and focus. `tests/test_theme_tokens.py` checks that every token
  exists in both themes and computes WCAG contrast for the declared pairs:
  4.5:1 for text, 3:1 for controls, focus and chart marks.

## Rules for UI code

- **Use `var(--token)`.** No hex, `rgb()` or named colours outside
  `tokens.css`, and no inline colour literals in markup or JS-built HTML.
  `tests/test_theme_ratchet.py` enforces this against the shrink-only
  `tests/theme_baseline.json`: per-file counts of hex, `rgb()`,
  `data-theme="dark"` overrides, `[style*=]` hacks and inline colours may only
  go down. New files must be colour-free. After removing debt, run
  `python tests/test_theme_ratchet.py --write`, which refuses to raise any number.
- **Never add a `:root[data-theme="dark"] …` override.** Use a token that
  already has both values.
- **Solid status fills carry page-colour text**: `background: var(--success-fg);
  color: var(--bg)`. That gives dark-on-light in the light theme and
  light-on-dark in the dark theme.
- **Status never relies on colour alone.** Gate dots have a shape per state
  (ring, disc, half disc, diamond, bullseye) and screen-reader text.
- **Emphasis comes from colour levels, not `opacity`.** Opacity drops text
  below AA contrast.
- **Modals go through `static/dialog.js`** (`openModal` / `closeModal` in
  `walkthrough.js` use it). It provides `role="dialog"`, `aria-modal`, focus
  in, a Tab trap, Escape, and focus restore.

## Third-party and generated surfaces

- **Plotly.** Live graphs are re-laid-out from the chart tokens on every theme
  change (`walkthrough.js` `_syncPlotlyTheme`), with transparent backgrounds.
  The generated comparative time-series document (`lib/comparative_viz.py`)
  inlines the boot, the tokens it uses and a live-sync script
  (`lib/theme_embed.py`). In the app it follows the theme; opened on its own it
  uses the default.
- **Server-rendered SVG charts** (`lib/study_charts.py`) use
  `style="fill:var(--chart-…, #light-fallback)"`, so exported reports without
  the tokens render exactly as before.
- **The investigation graph** (`aig-graph.js` string builder, DAG edges in
  `walkthrough.js`) uses tokens, so it re-colours live. d3 is loaded from the
  CDN but not used by workbench code.
- **bigraph-loom (vendored).**
  - `hooks/useResolvedTheme.ts` mirrors `data-theme` into React Flow's
    `colorMode`.
  - `App.css` and the panels' inline styles use `var(--token, #original)`, so
    the loom is unchanged standalone and themed inside the workbench.
  - React Flow's dark palette points at the same tokens.
  - Covered by the vitest `src/__tests__/theme.test.tsx`. Rebuild with
    `scripts/build_loom.sh`.
  - Coordinate these edits with upstream bigraph-loom.
- **The investigation report** (self-contained, with its own system-aware
  tokens). When served by the workbench over http(s) it opens in the
  workbench's explicit choice, its toggle writes `viv.theme`, and it follows
  changes live. A standalone `file://` copy behaves as before.
- **Documented exceptions:**
  - **Raster figures and unknown-content viz iframes** stay on `--figure-surface`
    (white in both themes) so they stay legible.
  - **The simularium / parsimony 3D viewers** are deliberately dark canvases in
    both themes.
  - **Data ramps** (the L0–L5 reproducibility grade in `audit.js`, Plotly
    series palettes) are fixed colours chosen to work in both themes. The
    grade pills keep white text at ≥4.9:1.
  - **The always-dark error toast** in `session-status.js`.
  - **Favicons.**
- **Native controls.** Text inputs, selects and textareas get `--field` and
  `--border-control` in dark mode. Checkboxes, radios and range inputs keep
  their native rendering, which `color-scheme: dark` draws dark.

## Accessibility

- **A global focus ring** (`--focus-ring`, ≥3:1) on every native control, `[tabindex]` element and ARIA widget role (`button`, `menuitem(radio)`, `tab`, `separator`).
- **Theme switches** briefly suppress transitions (`.viv-theme-switching`).
- **`prefers-reduced-motion`** shortens animations.
- **`forced-colors: active`** keeps borders and uses system colours.
- **axe-core runs in both themes** (`tests/e2e/test_e2e_a11y.py`, every WCAG
  2.0/2.1/2.2 A and AA rule). It covers the Registry, Catalog, Studies,
  Investigation, Study (including the Tests tab), Settings and About pages,
  plus the open theme menu and assistant panel. All are required to be
  violation-free.

## Tests

| Test | Covers |
|---|---|
| `tests/js/test_theme.js` | Resolution truth table; precedence (storage → cookie → default); invalid values; cookie flags; storage and OS events; `viv:themechange`; back-compat globals; Plotly adapter; boot/runtime equivalence |
| `tests/test_theme_boot.py` | The boot partial (size, no network, included first), the default (`system`) matching `theme.js`, and loom injection |
| `tests/test_theme_tokens.py` | Completeness and WCAG contrast |
| `tests/test_theme_ratchet.py` | Colour-debt ratchet |
| `tests/test_theme_embed.py` | Standalone generated documents |
| `tests/e2e/test_e2e_theme.py` | Browser checks (below) |

`tests/e2e/test_e2e_theme.py` checks:

- first-paint `data-theme` for each preference × OS scheme on every HTML entry
  point (FOUC probe);
- live OS changes;
- explicit choice beating the OS;
- persistence across reloads, tabs, the loom, and a server restart on a new port;
- the menu and radio group by keyboard;
- the loom `colorMode`;
- the generated viz document;
- modal dialog semantics.

Run the E2E suite with `VIVARIUM_WORKBENCH_E2E=1 pytest tests/e2e`, which
needs the `e2e` extra and `python -m playwright install chromium`. CI runs it
in `.github/workflows/e2e.yml`.
