/**
 * Async asset loading for PlayCanvas property tours.
 *
 * Assets live in the recon S3 bucket (see infra/reconstruction) and are served
 * through CloudFront. Splats land at `recon-outputs/<job>/model.splat`, which
 * the tour resolver (backend/tour_api.py) hands back as `splat_url`.
 *
 * Everything here is engine-typed but framework-free so it can be unit tested
 * without mounting React.
 */

import type * as pcNS from 'playcanvas';

export type TourAssetKind = 'gsplat' | 'model' | 'texture' | 'container';

/** An asset the viewer refuses to load, with the reason an operator can act on. */
export class UnsupportedTourAssetError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'UnsupportedTourAssetError';
  }
}

export interface TourAssetSpec {
  /** Stable id — also the PlayCanvas asset name. */
  id: string;
  /** Where the BYTES come from. May be a `blob:` URL. */
  url: string;
  /**
   * What the bytes ARE, when `url` cannot say.
   *
   * Splats delivered from `/api/media/{id}` are fetched with the Neoh JWT and
   * handed to PlayCanvas as a `blob:` URL, and a blob URL has no extension —
   * so format has to travel separately or every protected splat is parsed as
   * the wrong type. Set this to the original filename (`model.sog`) or the
   * original URL.
   */
  filename?: string;
  kind?: TourAssetKind;
  /** Optional label for the floor/level selector. */
  floor?: { id: string; name: string; index: number };
}

export interface LoadedTourAsset {
  spec: TourAssetSpec;
  asset: pcNS.Asset;
  entity: pcNS.Entity;
}

/**
 * Map a URL to the PlayCanvas asset type. Extension is authoritative.
 *
 * `.sog` is the delivery format, and `.splat` is still accepted because assets
 * recorded before the format fix are stored as `.splat` and render fine. The
 * pipeline used to target `.splat` alone, which turned out to be unreachable:
 * splat-transform lists `.splat` as input-only in every released version, so
 * the worker's conversion step failed for every provider that emits PLY. `.sog`
 * is what the tool writes and what PlayCanvas renders (`GSplatSogResource`
 * ships in the engine version this app already depends on).
 *
 * `.ply` is still deliberately NOT accepted. It is the raw training output: an
 * order of magnitude larger for the same scene, which on a phone means a long
 * stall on a metered connection before anything renders. Anything that produces
 * PLY must run splat-transform before the URL reaches a viewer, and failing
 * loudly here is how that stays true.
 */
export function inferAssetKind(url: string): TourAssetKind {
  // Strip query/hash first — presigned S3 URLs carry a long query string and
  // naive endsWith() checks fail on every signed asset.
  const path = url.split(/[?#]/, 1)[0].toLowerCase();
  if (path.endsWith('.ply')) {
    throw new UnsupportedTourAssetError(
      'PLY is not a delivery format — convert to .sog before serving it to a viewer.',
    );
  }
  if (path.endsWith('.sog') || path.endsWith('.splat')) return 'gsplat';
  if (path.endsWith('.glb') || path.endsWith('.gltf')) return 'container';
  if (/\.(png|jpe?g|webp|ktx2|basis)$/.test(path)) return 'texture';
  return 'model';
}

/**
 * Resolve a possibly-relative asset URL against the CDN.
 *
 * `VITE_TOUR_CDN_BASE` should be the CloudFront distribution domain. When the
 * backend already returns an absolute (often presigned) URL we pass it through
 * untouched — rewriting a presigned URL invalidates its signature.
 */
export function resolveAssetUrl(url: string): string {
  if (!url) return '';
  if (/^(https?:)?\/\//i.test(url) || url.startsWith('data:') || url.startsWith('blob:')) {
    return url;
  }
  const base = ((import.meta as any).env?.VITE_TOUR_CDN_BASE || '').replace(/\/$/, '');
  if (!base) return url;
  return `${base}${url.startsWith('/') ? '' : '/'}${url}`;
}

export interface LoadOptions {
  /** Aborts in-flight loads when the component unmounts mid-download. */
  signal?: AbortSignal;
  /** 0..1 across the whole batch. */
  onProgress?: (fraction: number) => void;
}

/**
 * Load one asset and attach it to the scene graph.
 *
 * PlayCanvas's AssetRegistry is callback-based; we promisify it and make the
 * promise abort-aware so a fast unmount doesn't leave a dangling handler that
 * touches a destroyed app.
 */
export function loadTourAsset(
  pc: typeof pcNS,
  app: pcNS.Application,
  spec: TourAssetSpec,
  options: LoadOptions = {},
): Promise<LoadedTourAsset> {
  const { signal } = options;
  const url = resolveAssetUrl(spec.url);

  return new Promise<LoadedTourAsset>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('aborted', 'AbortError'));
      return;
    }

    // Inside the executor so an unsupported format REJECTS rather than
    // throwing synchronously out of a function typed Promise<T>. The plural
    // loader awaits this and was therefore safe, but a direct caller using
    // .catch() would have taken an uncaught error — and this path became far
    // more reachable once a PLY could arrive as a filename hint behind a
    // blob: URL that hides the extension.
    let kind: TourAssetKind;
    try {
      // `spec.filename` first: a blob: URL carries no extension, so inferring
      // from the byte URL alone would silently mis-type every protected splat.
      kind = spec.kind ?? inferAssetKind(spec.filename || url);
    } catch (err) {
      reject(err);
      return;
    }

    // `filename` is part of PlayCanvas's file descriptor, not decoration: the
    // engine picks its parser from it when the URL cannot say. Without it a
    // blob: URL reaches the generic loader and the splat never renders.
    const file: { url: string; filename?: string } = { url };
    if (spec.filename) file.filename = spec.filename;
    const asset = new pc.Asset(spec.id, kind, file);

    let settled = false;
    const cleanup = () => {
      asset.off('load', onLoad);
      asset.off('error', onError);
      signal?.removeEventListener('abort', onAbort);
    };

    const onAbort = () => {
      if (settled) return;
      settled = true;
      cleanup();
      // Unload frees the GPU-side resource if the download already finished.
      // The entity is attached before the load starts, so an abort has to take
      // it back out or the scene keeps an empty splat entity forever.
      try { attached?.destroy(); } catch { /* app already destroyed */ }
      try { app.assets.remove(asset); asset.unload(); } catch { /* app already destroyed */ }
      reject(new DOMException('aborted', 'AbortError'));
    };

    const onLoad = () => {
      if (settled) return;
      settled = true;
      cleanup();

      try {
        const entity = attached ?? new pc.Entity(spec.id);

        if (kind === 'gsplat') {
          // Already attached before the load began — see below.
        } else if (kind === 'container') {
          // A GLB container instantiates its own hierarchy; use it directly so
          // animations and materials come along.
          const instance = (asset.resource as any).instantiateRenderEntity();
          entity.addChild(instance);
        } else {
          entity.addComponent('render', { asset });
        }

        if (!attached) app.root.addChild(entity);
        resolve({ spec, asset, entity });
      } catch (err) {
        reject(err instanceof Error ? err : new Error(String(err)));
      }
    };

    const onError = (err: string) => {
      if (settled) return;
      settled = true;
      cleanup();
      try { attached?.destroy(); } catch { /* app already destroyed */ }
      reject(new Error(`Failed to load ${spec.id}: ${err}`));
    };

    asset.once('load', onLoad);
    asset.once('error', onError);
    signal?.addEventListener('abort', onAbort, { once: true });

    app.assets.add(asset);

    // A gsplat component must exist BEFORE its asset finishes loading.
    // The component watches the asset through an AssetReference and builds its
    // instance when that reference reports a load; attaching afterwards means
    // the event has already gone by, so the component sits there holding a
    // perfectly good GSplatSogResource with `instance === null` and nothing
    // ever renders. That is a silent failure — the asset says loaded, the
    // component says it has an asset, and the scene stays black.
    let attached: pcNS.Entity | null = null;
    if (kind === 'gsplat') {
      attached = new pc.Entity(spec.id);
      attached.addComponent('gsplat', { asset });
      app.root.addChild(attached);
    }

    app.assets.load(asset);
  });
}

/**
 * Load a batch, reporting aggregate progress. Sequential rather than parallel:
 * splats are large and mobile Safari drops concurrent multi-hundred-MB fetches.
 */
export async function loadTourAssets(
  pc: typeof pcNS,
  app: pcNS.Application,
  specs: TourAssetSpec[],
  options: LoadOptions = {},
): Promise<LoadedTourAsset[]> {
  const loaded: LoadedTourAsset[] = [];
  for (let i = 0; i < specs.length; i += 1) {
    const result = await loadTourAsset(pc, app, specs[i], options);
    loaded.push(result);
    options.onProgress?.((i + 1) / specs.length);
  }
  return loaded;
}

/**
 * Fully release a loaded asset: detach from the graph, destroy the entity, and
 * drop both the CPU-side resource and its GPU buffers.
 *
 * Order matters. Destroying the entity first removes the component that holds
 * the last reference to the asset's GPU resource; unloading before that can
 * leave the renderer pointing at freed memory on some mobile drivers.
 */
export function releaseTourAsset(app: pcNS.Application, loaded: LoadedTourAsset): void {
  try {
    loaded.entity.destroy();
  } catch { /* already destroyed with the app */ }
  try {
    app.assets.remove(loaded.asset);
    loaded.asset.unload();
  } catch { /* registry already torn down */ }
}
