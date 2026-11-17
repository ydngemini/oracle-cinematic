// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import appSource from '../App.jsx?raw';

// Resolved lazily so a missing component is an assertion failure, not an import error.
const modules = import.meta.glob('./RouteFallback.jsx', { eager: true });

afterEach(cleanup);

describe('RouteFallback', () => {
  it('a loading route announces itself instead of rendering nothing', () => {
    // Every lazy route boundary must use the accessible fallback, never null.
    expect(appSource).not.toContain('fallback={null}');
    const suspenseCount = (appSource.match(/<Suspense\b/g) || []).length;
    const routeFallbackCount = (appSource.match(/<Suspense fallback=\{<RouteFallback\s*\/>\}/g) || []).length;
    expect(suspenseCount).toBe(4);
    expect(routeFallbackCount).toBe(suspenseCount);

    const mod = modules['./RouteFallback.jsx'];
    expect(mod).toBeTruthy();
    const RouteFallback = mod.default || mod.RouteFallback;
    expect(typeof RouteFallback).toBe('function');

    render(<RouteFallback />);
    const status = screen.getByRole('status');
    expect(status.getAttribute('aria-live')).toBe('polite');
    expect(status.textContent).toMatch(/loading/i);
  });
});
