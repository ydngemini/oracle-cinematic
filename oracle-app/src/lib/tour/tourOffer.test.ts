import { describe, expect, it } from 'vitest';
import { tourOffer } from './tourOffer';

describe('what may be offered', () => {
  it('offers a walk when a capture of this property exists', () => {
    const offer = tourOffer({ splat_url: '/public/splats/a.splat', is_this_property: true });
    expect(offer.kind).toBe('walkable');
    if (offer.kind === 'walkable') {
      expect(offer.isDemo).toBe(false);
      expect(offer.label).toMatch(/step inside/i);
    }
  });

  it('offers a walk over two or more 360s', () => {
    expect(tourOffer({ pano_scene_count: 2 }).kind).toBe('walkable');
  });

  it('refuses to call a single 360 a walkthrough', () => {
    // Mirrors isWalkable in ./panoGraph and the server's tier-2 rule: one
    // vantage point is a view, and there is no route from a place to itself.
    const offer = tourOffer({ pano_scene_count: 1 });
    expect(offer.kind).toBe('unavailable');
    if (offer.kind === 'unavailable') expect(offer.reason).toMatch(/at least two/i);
  });
});

describe('the demo-splat claim', () => {
  it('never says "step inside" over a space that is not this home', () => {
    const offer = tourOffer({ splat_url: '/public/splats/demo.splat', is_this_property: false });
    expect(offer.kind).toBe('walkable');
    if (offer.kind === 'walkable') {
      expect(offer.isDemo).toBe(true);
      expect(offer.label).toMatch(/not this home/i);
      // The specific failure this guards: a reader who skims the button and
      // believes they are looking at the property they are buying.
      expect(offer.label).not.toMatch(/step inside/i);
    }
  });

  it('does not mark it a demo when real 360s of this property carry the tour', () => {
    // is_this_property describes the splat. Real panos alongside it mean the
    // walk IS this house, and calling it a demo would understate the evidence.
    const offer = tourOffer({ splat_url: '/d.splat', is_this_property: false, pano_scene_count: 4 });
    expect(offer.kind).toBe('walkable');
    if (offer.kind === 'walkable') expect(offer.isDemo).toBe(false);
  });
});

describe('absence is stated, never implied by silence', () => {
  it('explains what is missing rather than reporting nothing', () => {
    const offer = tourOffer({ photo_count: 0 });
    expect(offer.kind).toBe('unavailable');
    if (offer.kind === 'unavailable') expect(offer.reason.trim().length).toBeGreaterThan(0);
  });

  it('counts the photos already on file so the surface is not called empty', () => {
    const offer = tourOffer({ photo_count: 7 });
    if (offer.kind === 'unavailable') expect(offer.reason).toMatch(/7 photos/);
  });

  it('says one photo, not "1 photos"', () => {
    const offer = tourOffer({ photo_count: 1 });
    if (offer.kind === 'unavailable') expect(offer.reason).toMatch(/1 photo\b/);
  });

  it('treats a resolver failure as unknown, not as "no tour"', () => {
    // useTour degrades to null when the fetch fails. Reporting that as "no 3D
    // tour yet" would state a fact about the property from a network error.
    const offer = tourOffer(null);
    expect(offer.kind).toBe('unavailable');
    if (offer.kind === 'unavailable') {
      expect(offer.reason).toMatch(/unavailable|could not reach/i);
      expect(offer.reason).not.toMatch(/no 3d tour yet/i);
    }
  });
});
