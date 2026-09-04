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

import { inferAssetKind, loadTourAsset, UnsupportedTourAssetError } from './assetLoader';

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

describe('protected media reaches PlayCanvas as bytes plus a format hint', () => {
  // The regression: the reconstruction worker stores finished splats behind
  // /api/media/{id}, which needs the Neoh JWT. PlayCanvas's own asset request
  // does not carry that header, so the URL had to stop being handed to it
  // directly. The bytes now arrive as a blob: URL — which has no extension —
  // so the format travels beside them or every protected splat is mis-parsed.

  it('infers gsplat from the filename when the byte URL is a blob', () => {
    expect(inferAssetKind('model.sog')).toBe('gsplat');
    // The blob URL alone says nothing, which is exactly why the hint exists.
    expect(inferAssetKind('blob:https://neoh.app/9f2c-4a1b')).toBe('model');
  });

  it('reads the hint from an /api/media path, not just a bare filename', () => {
    // TourViewer passes the ORIGINAL url as the hint, which is usually the
    // protected route rather than a filename.
    expect(inferAssetKind('/api/media/8d1e2f3a-1111-2222-3333-444455556666.sog'))
      .toBe('gsplat');
  });

  it('still refuses PLY when it arrives only as a hint', () => {
    // A blob URL would hide the extension entirely, so the guard has to sit on
    // the hint or raw training output could be handed to a phone.
    expect(() => inferAssetKind('model.ply')).toThrow(UnsupportedTourAssetError);
  });
});

describe('loadTourAsset builds the PlayCanvas file descriptor', () => {
  // Constructed against a minimal fake engine: the real one needs a WebGL
  // context, and what matters here is the descriptor, not the render.
  function fakeEngine() {
    const constructed: Array<{ id: string; kind: string; file: Record<string, string> }> = [];
    class FakeAsset {
      id: string;

      kind: string;

      file: Record<string, string>;

      handlers: Record<string, Array<(a?: unknown) => void>> = {};

      constructor(id: string, kind: string, file: Record<string, string>) {
        this.id = id;
        this.kind = kind;
        this.file = file;
        constructed.push({ id, kind, file });
      }

      once(event: string, fn: (a?: unknown) => void) {
        (this.handlers[event] ||= []).push(fn);
      }

      off() { /* no-op */ }

      unload() { /* no-op */ }
    }
    const app = {
      assets: {
        add() { /* no-op */ },
        load: (asset: FakeAsset) => {
          // Resolve on the next tick, the way a real load would.
          setTimeout(() => (asset.handlers.load || []).forEach((fn) => fn(asset)), 0);
        },
        remove() { /* no-op */ },
      },
      root: { addChild() { /* no-op */ } },
    };
    return { pc: { Asset: FakeAsset, Entity: class { addComponent() { /* no-op */ } } }, app, constructed };
  }

  it('passes the filename through so the engine can pick the SOG parser', async () => {
    const { pc, app, constructed } = fakeEngine();
    await loadTourAsset(pc as never, app as never, {
      id: 'property-splat',
      url: 'blob:https://neoh.app/9f2c-4a1b',
      filename: 'model.sog',
    });
    expect(constructed).toHaveLength(1);
    expect(constructed[0].kind).toBe('gsplat');
    expect(constructed[0].file.url).toBe('blob:https://neoh.app/9f2c-4a1b');
    expect(constructed[0].file.filename).toBe('model.sog');
  });

  it('omits filename entirely when there is none, rather than sending undefined', async () => {
    const { pc, app, constructed } = fakeEngine();
    await loadTourAsset(pc as never, app as never, {
      id: 'cdn-splat',
      url: 'https://cdn.example/recon/model.sog',
    });
    expect(constructed[0].kind).toBe('gsplat');
    expect('filename' in constructed[0].file).toBe(false);
  });

  it('refuses a PLY hint before any request is made', async () => {
    const { pc, app, constructed } = fakeEngine();
    await expect(loadTourAsset(pc as never, app as never, {
      id: 'raw',
      url: 'blob:https://neoh.app/abc',
      filename: 'point_cloud.ply',
    })).rejects.toBeInstanceOf(UnsupportedTourAssetError);
    expect(constructed).toHaveLength(0);
  });
});
