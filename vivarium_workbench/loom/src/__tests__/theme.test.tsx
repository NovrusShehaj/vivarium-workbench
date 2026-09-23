// @vitest-environment jsdom
// The loom follows the host workbench's resolved theme: <html data-theme> ->
// React Flow `colorMode` (which puts the `dark` class on the flow root).
import { describe, it, expect, beforeAll, afterEach } from 'vitest';
import { render, renderHook, act, cleanup, waitFor } from '@testing-library/react';
import App from '../App';
import { readResolvedTheme, useResolvedTheme } from '../hooks/useResolvedTheme';

beforeAll(() => {
  if (!('ResizeObserver' in globalThis)) {
    (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
});

afterEach(() => {
  cleanup();
  document.documentElement.removeAttribute('data-theme');
  window.history.pushState({}, '', '/');
});

describe('readResolvedTheme', () => {
  it('is light without a host theme (standalone loom)', () => {
    expect(readResolvedTheme()).toBe('light');
  });

  it('reads the resolved theme the workbench boot wrote', () => {
    document.documentElement.setAttribute('data-theme', 'dark');
    expect(readResolvedTheme()).toBe('dark');
    document.documentElement.setAttribute('data-theme', 'light');
    expect(readResolvedTheme()).toBe('light');
  });

  it('treats anything unexpected as light', () => {
    document.documentElement.setAttribute('data-theme', 'system');
    expect(readResolvedTheme()).toBe('light');
  });
});

describe('useResolvedTheme', () => {
  it('follows data-theme changes live', async () => {
    const { result } = renderHook(() => useResolvedTheme());
    expect(result.current).toBe('light');
    act(() => { document.documentElement.setAttribute('data-theme', 'dark'); });
    await waitFor(() => expect(result.current).toBe('dark'));
    act(() => { document.documentElement.setAttribute('data-theme', 'light'); });
    await waitFor(() => expect(result.current).toBe('light'));
  });

  it('re-reads on the workbench viv:themechange event', () => {
    const { result } = renderHook(() => useResolvedTheme());
    act(() => {
      document.documentElement.setAttribute('data-theme', 'dark');
      window.dispatchEvent(new CustomEvent('viv:themechange', { detail: { resolved: 'dark' } }));
    });
    expect(result.current).toBe('dark');
  });
});

describe('App colorMode', () => {
  function postCompositeLoad() {
    act(() => {
      window.dispatchEvent(new MessageEvent('message', {
        data: { type: 'composite:load', state: {}, metadata: { id: 'test.composites.demo', name: 'demo' } },
      }));
    });
  }

  it('passes the resolved theme to React Flow and follows switches', async () => {
    document.documentElement.setAttribute('data-theme', 'dark');
    const { container } = render(<App />);
    postCompositeLoad();
    const flow = await waitFor(() => {
      const el = container.querySelector('.react-flow');
      expect(el).toBeTruthy();
      return el as HTMLElement;
    });
    expect(flow.classList.contains('dark')).toBe(true);

    act(() => { document.documentElement.setAttribute('data-theme', 'light'); });
    await waitFor(() => expect(container.querySelector('.react-flow')!.classList.contains('dark')).toBe(false));
    expect(container.querySelector('.react-flow')!.classList.contains('light')).toBe(true);
  });

  it('stays light standalone', async () => {
    const { container } = render(<App />);
    postCompositeLoad();
    const flow = await waitFor(() => {
      const el = container.querySelector('.react-flow');
      expect(el).toBeTruthy();
      return el as HTMLElement;
    });
    expect(flow.classList.contains('dark')).toBe(false);
  });
});
