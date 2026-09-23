// src/hooks/useResolvedTheme.ts — the host page's resolved colour theme.
//
// Inside vivarium-workbench the loom document is served with the workbench's
// pre-paint theme boot, tokens.css and theme.js injected into <head>. The boot
// writes the RESOLVED theme to <html data-theme="light|dark">, and theme.js
// keeps it current (OS changes, other tabs, the parent shell's theme menu).
// This hook mirrors that attribute so React Flow can get a matching
// `colorMode`. It observes the attribute directly, so it follows every writer,
// and also listens for the workbench's `viv:themechange` event.
//
// Standalone (no host theme) the attribute is absent and the loom stays in
// its original light appearance.

import { useEffect, useState } from 'react';

export type ResolvedTheme = 'light' | 'dark';

export function readResolvedTheme(doc: Document | undefined = globalThis.document): ResolvedTheme {
  const v = doc?.documentElement?.getAttribute('data-theme');
  return v === 'dark' ? 'dark' : 'light';
}

export function useResolvedTheme(): ResolvedTheme {
  const [theme, setTheme] = useState<ResolvedTheme>(() => readResolvedTheme());

  useEffect(() => {
    const doc = globalThis.document;
    if (!doc?.documentElement) return undefined;
    const sync = () => setTheme(readResolvedTheme(doc));
    sync();
    let observer: MutationObserver | undefined;
    if (typeof MutationObserver !== 'undefined') {
      observer = new MutationObserver(sync);
      observer.observe(doc.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    }
    window.addEventListener('viv:themechange', sync);
    return () => {
      observer?.disconnect();
      window.removeEventListener('viv:themechange', sync);
    };
  }, []);

  return theme;
}
