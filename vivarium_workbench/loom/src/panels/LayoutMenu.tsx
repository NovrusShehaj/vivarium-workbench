// src/panels/LayoutMenu.tsx — toolbar "Layout" dropdown.
//
// Groups the layout controls that used to clutter the top bar: the flow
// direction (packing / top-to-bottom / left-to-right), Center-on-locked-process,
// Collapse-repeats, and Re-layout. Keeps the top bar to Layout · Detail · Views
// · Download.

import { useEffect, useRef, useState } from 'react';

const MODES = [
  { id: 'flow-down',  glyph: '↧', label: 'Tree',    t: 'Store place-graph as a vertical org-chart; processes packed into the hierarchy, in swimlanes between the store rows they bridge' },
  { id: 'tree-grid',  glyph: '▦', label: 'Grid',    t: 'Store org-chart on the left; every process pulled to the right in a scannable grid' },
  { id: 'hierarchy',  glyph: '○', label: 'Packing', t: 'Relationship packing — stores nestle among the processes that read/write them, no enforced direction' },
  { id: 'flow-right', glyph: '→', label: 'Flow',    t: 'Workflow DAG: stores → processes → stores, ranked left → right (ELK)' },
] as const;

export default function LayoutMenu(props: {
  modeId: string;
  setModeId: (id: string) => void;
  canCenter: boolean;
  onCenter: () => void;
  collapseRedundant: boolean;
  toggleCollapse: () => void;
  hyperedgeMode: boolean;
  toggleHyperedges: () => void;
  onRelayout: () => void;
}) {
  const { modeId, setModeId, canCenter, onCenter, collapseRedundant, toggleCollapse,
          hyperedgeMode, toggleHyperedges, onRelayout } = props;
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  const itemBtn: React.CSSProperties = {
    display: 'flex', alignItems: 'center', gap: 8, width: '100%', textAlign: 'left',
    padding: '7px 12px', fontSize: 12, border: 0, background: 'var(--surface, #fff)', cursor: 'pointer', color: 'var(--text, #374151)',
  };
  const hover = {
    onMouseEnter: (e: React.MouseEvent<HTMLElement>) => (e.currentTarget.style.background = 'var(--surface-3, #f3f4f6)'),
    onMouseLeave: (e: React.MouseEvent<HTMLElement>) => (e.currentTarget.style.background = 'var(--surface, #fff)'),
  };
  const active = MODES.find((m) => m.id === modeId) ?? MODES[0];

  return (
    <div ref={wrapRef} style={{ position: 'relative' }}>
      <button
        onClick={() => setOpen((v) => !v)}
        title="Layout — direction, center, collapse, re-layout"
        style={{
          height: 28, padding: '0 10px', fontSize: 12, background: open ? 'var(--info-bg, #eff6ff)' : 'var(--surface, #fff)',
          display: 'inline-flex', alignItems: 'center', gap: 5,
          border: '1px solid var(--border-2, #d1d5db)', borderRadius: 4, cursor: 'pointer',
          color: open ? 'var(--link, #2563eb)' : 'var(--text, #374151)', fontWeight: open ? 600 : 400,
        }}
      >
        <span style={{ fontSize: 14 }}>{active.glyph}</span> Layout ▾
      </button>

      {open && (
        <div style={{
          position: 'absolute', top: '100%', left: 0, marginTop: 4,
          background: 'var(--surface, #fff)', border: '1px solid var(--border-2, #d1d5db)', borderRadius: 4,
          boxShadow: '0 2px 10px rgba(0,0,0,0.14)', overflow: 'hidden',
          minWidth: 200, zIndex: 20,
        }}>
          {/* Re-layout FIRST — the most-reached-for action (re-runs auto-layout). */}
          <button
            onClick={() => { onRelayout(); setOpen(false); }}
            title="Re-run auto-layout on the currently visible nodes and fit the view"
            style={{ ...itemBtn, fontWeight: 600 }}
            {...hover}
          >
            <span style={{ width: 16, textAlign: 'center' }}>⟳</span>
            Re-layout
          </button>
          <div style={{ height: 1, background: 'var(--surface-3, #e5e7eb)', margin: '4px 0' }} />
          <div style={{ padding: '6px 12px 2px', fontSize: 10.5, color: 'var(--text-subtle, #9ca3af)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
            Arrange
          </div>
          {MODES.map((m) => (
            <button
              key={m.id}
              onClick={() => { setModeId(m.id); }}
              title={m.t}
              style={{ ...itemBtn, color: modeId === m.id ? 'var(--link, #2563eb)' : 'var(--text, #374151)', fontWeight: modeId === m.id ? 600 : 400 }}
              {...hover}
            >
              <span style={{ width: 16, textAlign: 'center', fontSize: 14 }}>{m.glyph}</span>
              {m.label}
              {modeId === m.id && <span style={{ marginLeft: 'auto', color: 'var(--link, #2563eb)' }}>✓</span>}
            </button>
          ))}
          <div style={{ height: 1, background: 'var(--surface-3, #e5e7eb)', margin: '4px 0' }} />
          <button
            onClick={() => { toggleCollapse(); }}
            title="Collapse topologically-identical repeated processes (e.g. dFBA[i,j]) into one representative with a count"
            style={{ ...itemBtn, color: collapseRedundant ? 'var(--link, #2563eb)' : 'var(--text, #374151)', fontWeight: collapseRedundant ? 600 : 400 }}
            {...hover}
          >
            <span style={{ width: 16, textAlign: 'center' }}>⊞</span>
            Collapse repeats
            {collapseRedundant && <span style={{ marginLeft: 'auto', color: 'var(--link, #2563eb)' }}>✓</span>}
          </button>
          <button
            onClick={() => { toggleHyperedges(); }}
            title="Milner view — replace each process with a hyperedge over the stores it connects (process bigraph → Milner bigraph)"
            style={{ ...itemBtn, color: hyperedgeMode ? 'var(--accent2, #7e22ce)' : 'var(--text, #374151)', fontWeight: hyperedgeMode ? 600 : 400 }}
            {...hover}
          >
            <span style={{ width: 16, textAlign: 'center' }}>⇢</span>
            Processes → hyperedges
            {hyperedgeMode && <span style={{ marginLeft: 'auto', color: 'var(--accent2, #7e22ce)' }}>✓</span>}
          </button>
          <button
            onClick={() => { if (canCenter) { onCenter(); setOpen(false); } }}
            disabled={!canCenter}
            title={canCenter
              ? 'Center the layout on the locked process (inputs left, outputs right, shared stores below)'
              : 'Lock a process first (click it, then the lock), then center on it'}
            style={{ ...itemBtn, cursor: canCenter ? 'pointer' : 'default', color: canCenter ? 'var(--text, #374151)' : '#b8bec9' }}
            {...(canCenter ? hover : {})}
          >
            <span style={{ width: 16, textAlign: 'center' }}>⊹</span>
            Center on locked process
          </button>
        </div>
      )}
    </div>
  );
}
