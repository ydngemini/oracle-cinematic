// @vitest-environment jsdom
/**
 * The real 3D corner, with PlayCanvas mocked: it reports the product state it
 * was given (so tests and the flat mascot can be compared with it), and every
 * engine failure is reported upward for the flat fallback rather than leaving
 * a blank canvas.
 */
import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AVATAR_STATES } from './avatarModel';

afterEach(() => {
  cleanup();
  vi.doUnmock('playcanvas');
  vi.resetModules();
});

async function load(engine) {
  vi.resetModules();
  vi.doMock('playcanvas', engine);
  return (await import('./NeohCorner3D.jsx')).NeohCorner3D;
}

describe('NeohCorner3D', () => {
  it('reports the state it was given, for each of the nine', async () => {
    const NeohCorner3D = await load(() => { throw new Error('chunk failed'); });
    const { container, rerender } = render(<NeohCorner3D state="idle" onFailure={() => {}} />);
    for (const state of AVATAR_STATES) {
      rerender(<NeohCorner3D state={state} actionType="call" onFailure={() => {}} />);
      const canvas = container.querySelector('canvas');
      expect(canvas.getAttribute('data-state')).toBe(state);
      expect(canvas.getAttribute('data-action')).toBe(state === 'acting' ? 'call' : null);
    }
    rerender(<NeohCorner3D state="dancing" onFailure={() => {}} />);
    expect(container.querySelector('canvas').getAttribute('data-state')).toBe('idle');
  });

  it('reports a chunk that failed to load', async () => {
    const NeohCorner3D = await load(() => { throw new Error('chunk failed'); });
    const onFailure = vi.fn();
    render(<NeohCorner3D state="thinking" onFailure={onFailure} />);
    await waitFor(() => expect(onFailure).toHaveBeenCalledWith('engine-unavailable'));
  });

  it('reports a machine with no WebGL', async () => {
    const NeohCorner3D = await load(() => ({
      Application: class { constructor() { throw new Error('WebGL not supported'); } },
    }));
    const onFailure = vi.fn();
    render(<NeohCorner3D state="thinking" onFailure={onFailure} />);
    await waitFor(() => expect(onFailure).toHaveBeenCalledWith('no-webgl'));
  });
});
