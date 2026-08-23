/**
 * Tour asset kind resolution.
 *
 * The rule this pins down: only a *delivery* format reaches a viewer. That is
 * `.sog` today, plus `.splat` for assets recorded before the format fix — both
 * render, so both are accepted.
 *
 * The pipeline used to target `.splat` alone, which turned out to be a format
 * splat-transform cannot write (it is input-only in every released version), so
 * conversion failed for every provider that emits PLY. `.sog` is what the tool
 * writes and what PlayCanvas renders.
 *
 * PLY stays refused: it is training output, roughly an order of magnitude larger
 * for the same scene, and reconstruction_worker converts it before anything is
 * served. Refusing it here keeps that invariant enforced at the boundary rather
 * than assumed.
 */

import { describe, expect, it } from 'vitest';

import { inferAssetKind, UnsupportedTourAssetError } from './assetLoader';

describe('inferAssetKind', () => {
  it('maps .sog to the gsplat component', () => {
    expect(inferAssetKind('https://cdn.example/recon/model.sog')).toBe('gsplat');
  });

  it('still maps legacy .splat assets to the gsplat component', () => {
    // Rows written before the delivery format changed are still served, and
    // still render. Dropping this would orphan every existing reconstruction.
    expect(inferAssetKind('https://cdn.example/recon/model.splat')).toBe('gsplat');
  });

  it('ignores the query string on a presigned .sog URL', () => {
    expect(
      inferAssetKind('https://cdn.example/recon/job-7/model.sog?X-Amz-Signature=abc'),
    ).toBe('gsplat');
  });

  it('ignores the query string on presigned URLs', () => {
    // A presigned S3 URL carries a long query; a naive endsWith() check fails on
    // every real asset the pipeline produces.
    expect(
      inferAssetKind('https://cdn.example/recon/job-7/model.splat?X-Amz-Signature=abc&X-Amz-Expires=900'),
    ).toBe('gsplat');
  });

  it('ignores a URL fragment', () => {
    expect(inferAssetKind('https://cdn.example/model.splat#floor-2')).toBe('gsplat');
  });

  it('maps mesh containers used by the floor-plan layout boxes', () => {
    expect(inferAssetKind('https://cdn.example/layout.glb')).toBe('container');
    expect(inferAssetKind('https://cdn.example/layout.gltf')).toBe('container');
  });

  it.each(['cover.jpg', 'cover.jpeg', 'cover.png', 'cover.webp', 'tex.ktx2', 'tex.basis'])(
    'maps %s to a texture',
    (file) => {
      expect(inferAssetKind(`https://cdn.example/${file}`)).toBe('texture');
    },
  );

  it('falls back to model for an unrecognised extension', () => {
    expect(inferAssetKind('https://cdn.example/mystery.bin')).toBe('model');
  });

  describe('PLY is refused', () => {
    it('throws rather than silently loading it', () => {
      expect(() => inferAssetKind('https://cdn.example/point_cloud.ply')).toThrow(
        UnsupportedTourAssetError,
      );
    });

    it('names the fix in the message', () => {
      // The operator needs to know to run splat-transform, not just that it
      // failed — and specifically to target .sog, since asking that tool for a
      // .splat is what silently broke the pipeline in the first place.
      expect(() => inferAssetKind('https://cdn.example/point_cloud.ply')).toThrow(/\.sog/);
    });

    it('is case-insensitive', () => {
      expect(() => inferAssetKind('https://cdn.example/OUT.PLY')).toThrow(UnsupportedTourAssetError);
    });

    it('still catches it behind a signature', () => {
      expect(() =>
        inferAssetKind('https://cdn.example/out.ply?X-Amz-Signature=deadbeef'),
      ).toThrow(UnsupportedTourAssetError);
    });

    it('does not false-positive on a filename merely containing "ply"', () => {
      expect(inferAssetKind('https://cdn.example/multiply-splat.splat')).toBe('gsplat');
    });
  });
});
