// @vitest-environment jsdom
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { NeohAvatar } from './NeohAvatar';
import { NeohCharacter } from './NeohCharacter';
import { VARIANTS, VARIANT_GEOMETRY } from './characterGeometry';
import { eyeExpression } from './eyeSystem';

// jsdom has no matchMedia, and the motion policy reaches for it.
vi.mock('./motion', () => ({
  useMotionPolicy: () => globalThis.__neohMotionPolicy || { reduced: false, layout: true, transition: {} },
}));

afterEach(cleanup);

/**
 * These test the rig's contracts, not its path data. Asserting raw `d`
 * strings would lock the artwork and break on every visual tweak while
 * catching nothing that matters — what matters is that the parts exist, are
 * separately addressable, and that each variant draws the right ones.
 */

const render3 = (variant, state = 'idle') =>
  render(<NeohCharacter variant={variant} expression={eyeExpression(state)} />).container;

describe('NeohCharacter rig', () => {
  it('draws the head on every variant', () => {
    for (const variant of VARIANTS) {
      const c = render3(variant);
      expect(c.querySelectorAll('ellipse, path').length).toBeGreaterThan(0);
      cleanup();
    }
  });

  it('uses each variant its own artboard', () => {
    for (const variant of VARIANTS) {
      const c = render3(variant);
      expect(c.querySelector('svg').getAttribute('viewBox')).toBe(VARIANT_GEOMETRY[variant].viewBox);
      cleanup();
    }
  });

  it('gives the head no transform of its own, and the others one', () => {
    // The head variant IS the 32x32 space; bust and full place it.
    expect(VARIANT_GEOMETRY.head.headTransform).toBeNull();
    expect(VARIANT_GEOMETRY.bust.headTransform).toContain('scale');
    expect(VARIANT_GEOMETRY.full.headTransform).toContain('scale');
  });

  it('draws a body only for bust and full', () => {
    const head = render3('head');
    const headParts = head.querySelectorAll('rect, ellipse, circle, path').length;
    cleanup();
    const bust = render3('bust');
    const bustParts = bust.querySelectorAll('rect, ellipse, circle, path').length;
    cleanup();
    const full = render3('full');
    const fullParts = full.querySelectorAll('rect, ellipse, circle, path').length;

    expect(bustParts).toBeGreaterThan(headParts);
    expect(fullParts).toBeGreaterThan(bustParts);
  });

  it('keeps every part independently addressable rather than one mega-path', () => {
    // A single clever path cannot tilt a head without dragging the shoulders.
    const c = render3('bust');
    expect(c.querySelectorAll('g').length).toBeGreaterThan(5);
  });

  it('falls back to the head artboard for an unknown variant', () => {
    const c = render(<NeohCharacter variant="nonsense" expression={eyeExpression('idle')} />).container;
    expect(c.querySelector('svg').getAttribute('viewBox')).toBe(VARIANT_GEOMETRY.head.viewBox);
  });

  it('is hidden from assistive tech at the svg level too', () => {
    const c = render3('full');
    expect(c.querySelector('svg').getAttribute('aria-hidden')).toBe('true');
  });
});

describe('NeohAvatar variants', () => {
  it('defaults to head', () => {
    const { container } = render(<NeohAvatar />);
    expect(container.querySelector('[data-variant]').getAttribute('data-variant')).toBe('head');
  });

  it('renders each variant and reports it on the wrapper', () => {
    for (const variant of VARIANTS) {
      const { container } = render(<NeohAvatar variant={variant} />);
      expect(container.querySelector('[data-variant]').getAttribute('data-variant')).toBe(variant);
      cleanup();
    }
  });

  it('passes size through as the height token for every variant', () => {
    for (const variant of VARIANTS) {
      const { container } = render(<NeohAvatar variant={variant} size="96px" />);
      expect(container.querySelector('[data-variant]').getAttribute('style')).toContain('96px');
      cleanup();
    }
  });

  it('keeps reporting state and action on the larger variants', () => {
    const { container } = render(<NeohAvatar variant="bust" state="acting" actionType="call" />);
    const node = container.querySelector('[data-variant]');
    expect(node.getAttribute('data-state')).toBe('acting');
    // The glyph is not a head-only affordance.
    expect(container.querySelector('svg.lucide-phone, .lucide-phone')).toBeTruthy();
  });

  it('clamps a nonsense audio level instead of trusting it', () => {
    const { container } = render(<NeohAvatar state="speaking" audioLevel={9} />);
    expect(container.querySelector('[data-variant]').getAttribute('style')).toContain('1.000');
    cleanup();
    const neg = render(<NeohAvatar state="speaking" audioLevel={-4} />).container;
    expect(neg.querySelector('[data-variant]').getAttribute('style')).toContain('0.000');
  });
});
