/**
 * Read splat centers + opacity out of whatever engine loaded them, as the
 * plain `SplatSamples` collision.ts builds its grid from.
 *
 * Duck-typed on purpose: this module must not import PlayCanvas (or gsplat),
 * so it can sit next to the pure collision maths and be unit-tested, and so
 * nothing here can drag an engine into the entry chunk.
 */

import type { SplatSamples } from './collision';

/** The parts of a .sog `meta.json` that decide how opacity is stored. */
export interface SogMeta {
  version?: number;
  sh0?: { mins?: number[]; maxs?: number[]; codebook?: unknown } | null;
}

/**
 * byte → opacity (0..1) for a .sog sh0 texture's alpha channel.
 *
 * SOG v2 stores opacity directly as alpha / 255. SOG v1 stores a quantised
 * logit between meta.sh0.mins[3] and maxs[3], so opacity is
 * sigmoid(lerp(min, max, byte / 255)) — the same decode PlayCanvas's own
 * GSplatSogIterator uses. Null when the meta does not say (no opacity
 * rather than a guessed one).
 */
export function sogOpacityLut(meta: SogMeta | null | undefined): Float32Array | null {
  if (!meta) return null;
  const lut = new Float32Array(256);
  if (meta.version === 2) {
    for (let b = 0; b < 256; b += 1) lut[b] = b / 255;
    return lut;
  }
  const lo = Number(meta.sh0?.mins?.[3]);
  const hi = Number(meta.sh0?.maxs?.[3]);
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null;
  for (let b = 0; b < 256; b += 1) {
    const logit = lo + (hi - lo) * (b / 255);
    lut[b] = 1 / (1 + Math.exp(-logit));
  }
  return lut;
}

interface ReadableTexture {
  width: number;
  height: number;
  read?: (x: number, y: number, w: number, h: number, options?: Record<string, unknown>) => Promise<ArrayLike<number>> | undefined;
}

/**
 * Samples from a loaded PlayCanvas GSplat resource (`asset.resource`).
 *
 * Centers come from `resource.centers` (what the renderer sorts by; the same
 * source the viewer frames with). For .sog, opacity is the sh0 texture's alpha,
 * read back from the GPU once — asynchronously (PlayCanvas uses a PBO + fence),
 * so it does not stall a frame. If the readback is unavailable or fails, the
 * grid is built from centers alone and says so (`opacity: 'none'`).
 *
 * Null when there are no centers at all (a mesh, or an engine path that does
 * not keep them): no splat data, no collision.
 */
export async function samplesFromPlayCanvasResource(
  resource: unknown,
  options: { signal?: AbortSignal; readTimeoutMs?: number } = {},
): Promise<SplatSamples | null> {
  const res = resource as {
    hasCenters?: boolean;
    centers?: Float32Array | null;
    gsplatData?: { isSog?: boolean; numSplats?: number; meta?: SogMeta; sh0?: ReadableTexture | null } | null;
  } | null;
  if (!res || res.hasCenters === false) return null;
  const centers = res.centers;
  if (!centers || centers.length < 3) return null;
  const data = res.gsplatData;
  const count = Math.min(Math.floor(centers.length / 3), Number(data?.numSplats) || Infinity);
  const samples: SplatSamples = { centers, count };

  if (data?.isSog && data.sh0 && typeof data.sh0.read === 'function') {
    const lut = sogOpacityLut(data.meta);
    if (lut) {
      try {
        const tex = data.sh0;
        const pending = tex.read?.(0, 0, tex.width, tex.height, { mipLevel: 0, face: 0, immediate: true });
        const bytes = pending ? await withTimeout(pending, options.readTimeoutMs ?? 5000) : null;
        if (!options.signal?.aborted && bytes && bytes.length >= count * 4) {
          samples.opacityBytes = bytes;
          samples.opacityStride = 4;
          samples.opacityOffset = 3;
          samples.opacityLut = lut;
        }
      } catch {
        /* readback unsupported here — centers alone */
      }
    }
  }
  return samples;
}

/** Samples from a gsplat.js `Splat` (`splat.data`): RGBA colours carry opacity. */
export function samplesFromGsplatData(data: unknown): SplatSamples | null {
  const d = data as { positions?: Float32Array; colors?: Uint8Array; vertexCount?: number } | null;
  if (!d?.positions || d.positions.length < 3) return null;
  const count = Math.min(Math.floor(d.positions.length / 3), Number(d.vertexCount) || Infinity);
  const colors = d.colors && d.colors.length >= count * 4 ? d.colors : null;
  return {
    centers: d.positions,
    count,
    opacityBytes: colors,
    opacityStride: 4,
    opacityOffset: 3,
  };
}

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T | null> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => resolve(null), ms);
    promise.then(
      (value) => { clearTimeout(timer); resolve(value); },
      (err) => { clearTimeout(timer); reject(err); },
    );
  });
}
