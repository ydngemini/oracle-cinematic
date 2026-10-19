/**
 * Live pixel hints for Capture Studio (prompt 12, T12.03; ledger L12-03).
 *
 * The same three measures, at the same analysis size and thresholds, as the
 * server's capture gate (backend/capture_quality.py: ANALYSIS_EDGE, DARK_LUMA,
 * BLUR_ABSOLUTE, DUPLICATE_DIFF; the same 4-neighbour Laplacian over the
 * interior), so a hint on the phone and a finding at the gate mean the same
 * thing. Two small differences, stated: luma is computed in floating point
 * (PIL rounds), and the 16x16 duplicate thumbnail is a box average (PIL
 * resamples bicubically). They are HINTS: computed on a sampled preview
 * frame, in the browser, before anything is uploaded. Only the server gate and
 * the post-build metrics may say a capture is usable — never this file.
 *
 * Pure functions over an ImageData-like {width, height, data: RGBA bytes}; the
 * worker (qualityWorker.js) runs them off the main thread.
 */

/** Edge the statistics are computed at (capture_quality.ANALYSIS_EDGE). */
export const ANALYSIS_EDGE = 512;
/** Mean luminance (0-255) below which a frame is effectively black. */
export const DARK_LUMA = 22.0;
/** Laplacian variance at ANALYSIS_EDGE below which a frame is blurred beyond use. */
export const BLUR_ABSOLUTE = 12.0;
/** Mean absolute difference between consecutive 16x16 thumbnails: same view. */
export const DUPLICATE_DIFF = 1.6;
const THUMB = 16;

/** Size to draw a w×h frame at so its long edge is ANALYSIS_EDGE (never upscaled). */
export function analysisSize(width, height) {
  const long = Math.max(width, height);
  if (!long) return { width: 0, height: 0 };
  const scale = Math.min(1, ANALYSIS_EDGE / long);
  return { width: Math.max(1, Math.round(width * scale)), height: Math.max(1, Math.round(height * scale)) };
}

/** Rec. 601 luma per pixel, as a Float32Array. */
export function toLuma({ width, height, data }) {
  const out = new Float32Array(width * height);
  for (let i = 0, p = 0; i < out.length; i += 1, p += 4) {
    out[i] = 0.299 * data[p] + 0.587 * data[p + 1] + 0.114 * data[p + 2];
  }
  return out;
}

/** Variance of the 4-neighbour Laplacian (OpenCV's CV_64F Laplacian, ksize 1). */
export function laplacianVariance(luma, width, height) {
  if (width < 3 || height < 3) return 0;
  let sum = 0;
  let sumSq = 0;
  let n = 0;
  for (let y = 1; y < height - 1; y += 1) {
    for (let x = 1; x < width - 1; x += 1) {
      const i = y * width + x;
      const v = luma[i - 1] + luma[i + 1] + luma[i - width] + luma[i + width] - 4 * luma[i];
      sum += v;
      sumSq += v * v;
      n += 1;
    }
  }
  const mean = sum / n;
  return sumSq / n - mean * mean;
}

/** 16×16 box-averaged thumbnail of the luma plane. */
export function thumbnail(luma, width, height) {
  const out = new Float32Array(THUMB * THUMB);
  for (let ty = 0; ty < THUMB; ty += 1) {
    const y0 = Math.floor((ty * height) / THUMB);
    const y1 = Math.max(y0 + 1, Math.floor(((ty + 1) * height) / THUMB));
    for (let tx = 0; tx < THUMB; tx += 1) {
      const x0 = Math.floor((tx * width) / THUMB);
      const x1 = Math.max(x0 + 1, Math.floor(((tx + 1) * width) / THUMB));
      let s = 0;
      let c = 0;
      for (let y = y0; y < y1 && y < height; y += 1) {
        for (let x = x0; x < x1 && x < width; x += 1) { s += luma[y * width + x]; c += 1; }
      }
      out[ty * THUMB + tx] = c ? s / c : 0;
    }
  }
  return out;
}

export function thumbDiff(a, b) {
  if (!a || !b || a.length !== b.length) return Infinity;
  let s = 0;
  for (let i = 0; i < a.length; i += 1) s += Math.abs(a[i] - b[i]);
  return s / a.length;
}

/** Statistics for one frame, at whatever size it was drawn. */
export function frameStats(image) {
  const luma = toLuma(image);
  let total = 0;
  for (let i = 0; i < luma.length; i += 1) total += luma[i];
  return {
    width: image.width,
    height: image.height,
    meanLuma: luma.length ? total / luma.length : 0,
    sharpness: laplacianVariance(luma, image.width, image.height),
    thumb: thumbnail(luma, image.width, image.height),
  };
}

/**
 * The hints for a frame, given the previous frame's stats. Each is a boolean
 * plus the measured value; `analysedAtEdge` is false when the frame was
 * smaller than ANALYSIS_EDGE, where the blur threshold was not calibrated.
 */
export function frameHints(stats, previous = null) {
  const longEdge = Math.max(stats.width, stats.height);
  const diff = previous ? thumbDiff(stats.thumb, previous.thumb) : Infinity;
  return {
    dark: stats.meanLuma < DARK_LUMA,
    blurred: stats.sharpness < BLUR_ABSOLUTE,
    duplicate: diff < DUPLICATE_DIFF,
    meanLuma: Math.round(stats.meanLuma * 10) / 10,
    sharpness: Math.round(stats.sharpness * 10) / 10,
    analysedAtEdge: longEdge >= ANALYSIS_EDGE,
  };
}

/** What to tell the person holding the phone, worst first; null when nothing. */
export function hintMessage(hints) {
  if (!hints) return null;
  if (hints.dark) return 'Too dark here — turn on lights or open blinds.';
  if (hints.blurred) return 'Blurry — slow down and turn more gently.';
  if (hints.duplicate) return 'Same view as a moment ago — keep moving slowly.';
  return null;
}
