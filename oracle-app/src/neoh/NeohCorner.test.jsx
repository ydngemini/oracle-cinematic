// @vitest-environment jsdom
/**
 * Home must not download the 3D engine to be useful (Mission 3 §56).
 *
 * The corner mascot lazy-loaded NeohCorner3D — which pulls PlayCanvas, the
 * largest chunk in the build (~2.3 MB) — on every Home mount of any capable
 * machine, including phones and tablets where the corner is display:none.
 * Now the SVG Neoh is the default and the 3D one is fetched only after a long
 * dwell on a desktop-width, capable machine with a connection that allows it.
 */

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AVATAR_STATES } from './avatarModel';
import { publishAvatar, resetAvatarPresence, withdrawAvatar } from './avatarPresence';
import { NeohCorner } from './NeohCorner';

vi.mock('./motion', () => ({
  useMotionPolicy: () => ({ reduced: false, layout: true, transition: {} }),
}));
vi.mock('./NeohCorner3D.jsx', () => {
  globalThis.__corner3dImported = true;
  return {
    default: ({ state, actionType, attentionLevel, still, onFailure }) => {
      globalThis.__corner3dFail = onFailure;
      return (
        <canvas
          data-testid="neoh-3d"
          data-state={state}
          data-action={actionType}
          data-attention={String(attentionLevel)}
          data-still={String(still)}
        />
      );
    },
  };
});

const here = dirname(fileURLToPath(import.meta.url));

function environment({ wide = true, saveData = false, effectiveType = '4g' } = {}) {
  window.matchMedia = (query) => ({
    matches: query.includes('min-width: 1100px') ? wide : false,
    media: query,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  });
  Object.defineProperty(navigator, 'connection', { configurable: true, value: { saveData, effectiveType } });
  Object.defineProperty(navigator, 'hardwareConcurrency', { configurable: true, value: 8 });
  Object.defineProperty(navigator, 'deviceMemory', { configurable: true, value: 8 });
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  resetAvatarPresence();
  vi.useRealTimers();
});

const view = (state, extra = {}) => ({
  state, actionType: 'generic', audioLevel: 0, attentionLevel: state === 'needs_attention' ? 1 : 0, reason: null, ...extra,
});
const flatState = (container) => container.querySelector('[data-state]')?.getAttribute('data-state');

describe('NeohCorner', () => {
  // Negative cases first: once the mocked 3D module has been imported in this
  // file the flag stays set, so these must run before the positive case.
  it('never loads the 3D mascot where the corner is hidden (phones, tablets)', () => {
    environment({ wide: false });
    const { container } = render(<NeohCorner />);
    act(() => { vi.advanceTimersByTime(120_000); });
    expect(globalThis.__corner3dImported).toBeUndefined();
    expect(container.querySelector('svg')).toBeTruthy(); // the SVG Neoh is there
  });

  it('never loads it on save-data or a slow connection', () => {
    environment({ saveData: true });
    render(<NeohCorner />);
    act(() => { vi.advanceTimersByTime(120_000); });
    cleanup();
    environment({ effectiveType: '3g' });
    render(<NeohCorner />);
    act(() => { vi.advanceTimersByTime(120_000); });
    expect(globalThis.__corner3dImported).toBeUndefined();
  });

  // AC15.09 (flat half): these run on a narrow screen, so the 3D module is
  // still unimported for the dwell test below.
  it('shows each of the nine states it is given on the flat mascot', () => {
    environment({ wide: false });
    for (const state of AVATAR_STATES) {
      const { container, unmount } = render(<NeohCorner avatar={view(state)} />);
      expect(flatState(container), state).toBe(state);
      unmount();
    }
    expect(globalThis.__corner3dImported).toBeUndefined();
  });

  it('follows the state the Neoh surface shares, and rests at idle without one', () => {
    environment({ wide: false });
    const { container } = render(<NeohCorner />);
    expect(flatState(container)).toBe('idle');
    act(() => { publishAvatar('surface', view('thinking')); });
    expect(flatState(container)).toBe('thinking');
    act(() => { publishAvatar('surface', view('acting', { actionType: 'call' })); });
    expect(flatState(container)).toBe('acting');
    act(() => { withdrawAvatar('surface'); });
    expect(flatState(container)).toBe('idle');
  });

  it('does not import the 3D engine on mount, only after a long dwell on a capable desktop', async () => {
    environment();
    const { container } = render(<NeohCorner />);
    act(() => { vi.advanceTimersByTime(10_000); });
    expect(globalThis.__corner3dImported).toBeUndefined();
    expect(container.querySelector('svg')).toBeTruthy();

    act(() => { vi.advanceTimersByTime(25_000); });
    vi.useRealTimers();
    expect(await screen.findByTestId('neoh-3d')).toBeTruthy();
    expect(globalThis.__corner3dImported).toBe(true);
  });

  it('hands the 3D mascot exactly the state the flat one shows, for all nine (AC15.09)', async () => {
    environment();
    const { rerender } = render(<NeohCorner avatar={view('idle')} />);
    act(() => { vi.advanceTimersByTime(35_000); });
    vi.useRealTimers();
    const canvas = await screen.findByTestId('neoh-3d');
    for (const state of AVATAR_STATES) {
      rerender(<NeohCorner avatar={view(state, { actionType: 'message' })} />);
      expect(canvas.getAttribute('data-state'), state).toBe(state);
      expect(canvas.getAttribute('data-attention')).toBe(state === 'needs_attention' ? '1' : '0');
      expect(canvas.getAttribute('data-still')).toBe('false');
    }
    // And when the 3D one fails, the flat one takes over in the SAME state.
    rerender(<NeohCorner avatar={view('needs_attention')} />);
    act(() => { globalThis.__corner3dFail('context-lost'); });
    expect(screen.queryByTestId('neoh-3d')).toBeNull();
    expect(document.querySelector('[data-state]').getAttribute('data-state')).toBe('needs_attention');
  });

  it('no file on the Home path imports PlayCanvas', () => {
    // Home = the shell, the briefing, the corner, the floating composer.
    // Only NeohCorner3D (behind the dwell above) may reach for the engine.
    for (const file of [
      '../components/CrmShell.jsx', 'NeohHome.jsx', 'NeohCorner.jsx', 'NeohSurface.jsx',
      'NeohComposer.jsx', 'NeohAvatar.jsx', 'NeohConversation.jsx',
    ]) {
      const source = readFileSync(join(here, file), 'utf8');
      expect(source, file).not.toMatch(/from ['"]playcanvas['"]|import\(['"]playcanvas['"]\)/);
    }
  });
});
