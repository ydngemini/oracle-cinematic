/**
 * What this device can honestly be asked to render.
 *
 * Neoh Space must never assume gigabit Wi-Fi and a desktop GPU. Before a 3D
 * space is mounted we decide — from signals the browser actually exposes —
 * whether to try at all, and at which quality level. Automatic only: nothing
 * here is a knob a person has to understand.
 *
 * Pure: `assessDevice` takes a snapshot of the environment so it can be tested
 * without a browser; `probeDevice` reads the real one.
 */

export type QualityLevel = 'high' | 'balanced' | 'performance';

export interface DeviceSnapshot {
  webgl2: boolean;
  webgl: boolean;
  /** navigator.deviceMemory, GB (Chromium only; undefined elsewhere). */
  deviceMemoryGb?: number;
  hardwareConcurrency?: number;
  /** Network Information API, where present. */
  saveData?: boolean;
  effectiveType?: string;
  downlinkMbps?: number;
  touch: boolean;
  devicePixelRatio: number;
  /** The renderer string, when WEBGL_debug_renderer_info is available. */
  renderer?: string;
}

export interface DeviceAssessment {
  /** False means: do not mount the 3D viewer; show the fallback. */
  canRender3D: boolean;
  /** Product-language reason when it cannot, or when it is degraded. */
  reason: string | null;
  quality: QualityLevel;
  /** Cap on the canvas pixel ratio for this quality. */
  maxPixelRatio: number;
  /** The network is slow or metered: ask before downloading a large space. */
  constrainedNetwork: boolean;
}

/** Pixel-ratio cap per level. DPR is the biggest lever on phone heat and OOM. */
export const PIXEL_RATIO: Record<QualityLevel, number> = {
  high: 2,
  balanced: 1.5,
  performance: 1,
};

const SOFTWARE_RENDERERS = /swiftshader|llvmpipe|software|basic render/i;

export function assessDevice(env: DeviceSnapshot): DeviceAssessment {
  if (!env.webgl2 && !env.webgl) {
    return {
      canRender3D: false,
      reason: '3D view unavailable on this device.',
      quality: 'performance',
      maxPixelRatio: 1,
      constrainedNetwork: false,
    };
  }
  if (env.renderer && SOFTWARE_RENDERERS.test(env.renderer)) {
    // A software rasteriser "supports" WebGL at a frame every few seconds.
    return {
      canRender3D: false,
      reason: '3D view unavailable on this device (no graphics acceleration).',
      quality: 'performance',
      maxPixelRatio: 1,
      constrainedNetwork: false,
    };
  }

  // Data Saver, or a connection the browser itself classes as 3G or worse.
  // `downlink` is deliberately not used: browsers round and cap it, and a
  // headless or freshly-woken phone reports ~1.5 Mbps on a fast link.
  const constrainedNetwork = Boolean(
    env.saveData
      || (env.effectiveType && /^(slow-2g|2g|3g)$/.test(env.effectiveType)),
  );

  const memory = env.deviceMemoryGb;
  const cores = env.hardwareConcurrency;
  let quality: QualityLevel;
  if ((memory !== undefined && memory <= 2) || (cores !== undefined && cores <= 2)) {
    quality = 'performance';
  } else if (env.touch || (memory !== undefined && memory <= 4) || !env.webgl2) {
    quality = 'balanced';
  } else {
    quality = 'high';
  }

  return {
    canRender3D: true,
    reason: quality === 'performance' ? 'Showing a lighter 3D view for this device.' : null,
    quality,
    maxPixelRatio: Math.min(env.devicePixelRatio || 1, PIXEL_RATIO[quality]),
    constrainedNetwork,
  };
}

/** Read the real environment. Never throws; a probe failure means "unknown". */
export function probeDevice(): DeviceSnapshot {
  const nav = (typeof navigator !== 'undefined' ? navigator : {}) as Navigator & {
    deviceMemory?: number;
    connection?: { saveData?: boolean; effectiveType?: string; downlink?: number };
  };
  let webgl2 = false;
  let webgl = false;
  let renderer: string | undefined;
  try {
    const canvas = document.createElement('canvas');
    const gl2 = canvas.getContext('webgl2') as WebGL2RenderingContext | null;
    const gl = (gl2 || canvas.getContext('webgl')) as WebGLRenderingContext | null;
    webgl2 = Boolean(gl2);
    webgl = Boolean(gl);
    if (gl) {
      const info = gl.getExtension('WEBGL_debug_renderer_info');
      if (info) renderer = String(gl.getParameter(info.UNMASKED_RENDERER_WEBGL) || '');
      // Free the probe context immediately: iOS caps live contexts per page.
      gl.getExtension('WEBGL_lose_context')?.loseContext();
    }
  } catch {
    /* no canvas / blocked — treated as no WebGL */
  }
  return {
    webgl2,
    webgl,
    deviceMemoryGb: typeof nav.deviceMemory === 'number' ? nav.deviceMemory : undefined,
    hardwareConcurrency: typeof nav.hardwareConcurrency === 'number' ? nav.hardwareConcurrency : undefined,
    saveData: nav.connection?.saveData,
    effectiveType: nav.connection?.effectiveType,
    downlinkMbps: nav.connection?.downlink,
    touch: typeof window !== 'undefined' && ('ontouchstart' in window || (nav.maxTouchPoints ?? 0) > 0),
    devicePixelRatio: typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1,
    renderer,
  };
}
