// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { NeohAvatar } from './NeohAvatar';
import { actionInput, stateInput } from './riveInputs';

vi.mock('./motion', () => ({
  useMotionPolicy: () => globalThis.__neohMotionPolicy || { reduced: false, layout: true, transition: {} },
}));

afterEach(cleanup);

function avatarNode(container) {
  return container.querySelector('[data-state]');
}

describe('NeohAvatar', () => {
  it('renders the fallback face when no Rive asset is configured', () => {
    const { container } = render(<NeohAvatar state="idle" />);
    // A real drawn face, not an empty placeholder box.
    expect(container.querySelector('svg')).toBeTruthy();
    expect(container.querySelectorAll('ellipse').length).toBe(2); // two eyes
  });

  it.each([
    'idle', 'listening', 'thinking', 'speaking',
    'acting', 'success', 'needs_attention', 'error', 'disconnected',
  ])('exposes %s as a semantic state attribute', (state) => {
    const { container } = render(<NeohAvatar state={state} />);
    expect(avatarNode(container).getAttribute('data-state')).toBe(state);
  });

  it('draws arc eyes for success instead of round ones', () => {
    const { container } = render(<NeohAvatar state="success" />);
    expect(container.querySelectorAll('ellipse').length).toBe(0);
    expect(container.querySelectorAll('path').length).toBeGreaterThan(2);
  });

  it('shows a call glyph while acting on a call', () => {
    const { container } = render(<NeohAvatar state="acting" actionType="call" />);
    expect(container.querySelector('svg.lucide-phone')).toBeTruthy();
  });

  it('shows a message glyph while acting on a message', () => {
    const { container } = render(<NeohAvatar state="acting" actionType="message" />);
    expect(container.querySelector('svg.lucide-message-square')).toBeTruthy();
  });

  it('shows no action glyph when not acting', () => {
    const { container } = render(<NeohAvatar state="thinking" actionType="call" />);
    expect(container.querySelector('svg.lucide-phone')).toBeNull();
  });

  it('passes amplitude through as a CSS custom property while speaking', () => {
    const { container } = render(<NeohAvatar state="speaking" audioLevel={0.6} />);
    const svg = container.querySelector('svg');
    expect(svg.getAttribute('style')).toContain('0.600');
  });

  it('is hidden from screen readers — the surface already announces status', () => {
    const { container } = render(<NeohAvatar state="thinking" />);
    expect(avatarNode(container).getAttribute('aria-hidden')).toBe('true');
    // And nothing inside it is reachable as a labelled element.
    expect(screen.queryByRole('img')).toBeNull();
  });

  it('holds still under reduced motion but keeps reporting the state', () => {
    globalThis.__neohMotionPolicy = { reduced: true, layout: false, transition: {} };
    try {
      const { container } = render(<NeohAvatar state="thinking" audioLevel={0.9} />);
      const node = avatarNode(container);
      expect(node.getAttribute('data-still')).toBe('true');
      // The state is still legible — reduced motion must not hide Neoh.
      expect(node.getAttribute('data-state')).toBe('thinking');
      // Amplitude is zeroed so nothing is driven frame by frame.
      expect(container.querySelector('svg').getAttribute('style')).toContain('0.000');
    } finally {
      globalThis.__neohMotionPolicy = undefined;
    }
  });

  it('supports head/bust/full variants, defaulting to head', () => {
    const { container: head } = render(<NeohAvatar state="idle" />);
    expect(avatarNode(head).getAttribute('data-variant')).toBe('head');
    const { container: full } = render(<NeohAvatar state="idle" variant="full" />);
    expect(avatarNode(full).getAttribute('data-variant')).toBe('full');
  });
});

describe('Rive input contract', () => {
  it('maps every semantic state to a stable numeric input', () => {
    expect(stateInput('idle')).toBe(0);
    expect(stateInput('listening')).toBe(1);
    expect(stateInput('thinking')).toBe(2);
    expect(stateInput('speaking')).toBe(3);
    expect(stateInput('acting')).toBe(4);
    expect(stateInput('success')).toBe(5);
    expect(stateInput('needs_attention')).toBe(6);
    expect(stateInput('error')).toBe(7);
    expect(stateInput('disconnected')).toBe(8);
  });

  it('falls back to idle for an unknown state rather than throwing', () => {
    expect(stateInput('nonsense')).toBe(0);
    expect(actionInput('nonsense')).toBe(0);
  });

  it('maps action types to stable numeric inputs', () => {
    expect(actionInput('call')).toBe(1);
    expect(actionInput('message')).toBe(2);
    expect(actionInput('share')).toBe(7);
  });
});
