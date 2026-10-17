import { describe, expect, it } from 'vitest';

import { cameraErrorState, cameraSupport } from './cameraSupport';
import { StorageRefused, createCaptureStore, memoryAdapter } from './captureStore';
import {
  ANALYSIS_EDGE, BLUR_ABSOLUTE, DARK_LUMA, analysisSize, frameHints, frameStats, hintMessage,
} from './frameQuality';
import { HintScheduler, MAX_INTERVAL_MS, MIN_INTERVAL_MS } from './hintScheduler';

function image(width, height, pixel) {
  const data = new Uint8ClampedArray(width * height * 4);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const v = pixel(x, y);
      const p = (y * width + x) * 4;
      data[p] = v; data[p + 1] = v; data[p + 2] = v; data[p + 3] = 255;
    }
  }
  return { width, height, data };
}

describe('frame hints use the server gate thresholds (capture_quality.py)', () => {
  it('mirrors the gate constants', () => {
    expect([ANALYSIS_EDGE, DARK_LUMA, BLUR_ABSOLUTE]).toEqual([512, 22, 12]);
  });

  it('draws at the analysis edge and never upscales', () => {
    expect(analysisSize(1920, 1080)).toEqual({ width: 512, height: 288 });
    expect(analysisSize(320, 240)).toEqual({ width: 320, height: 240 });
  });

  it('flags a black frame as dark and a flat frame as blurred', () => {
    const h = frameHints(frameStats(image(64, 48, () => 5)));
    expect(h.dark).toBe(true);
    expect(h.blurred).toBe(true);
    expect(hintMessage(h)).toMatch(/Too dark/);
  });

  it('a sharp, lit frame raises nothing; the same frame again is a repeat', () => {
    const sharp = frameStats(image(64, 48, (x, y) => ((x + y) % 2 ? 230 : 40)));
    const h = frameHints(sharp);
    expect(h.dark || h.blurred || h.duplicate).toBe(false);
    expect(hintMessage(h)).toBeNull();
    expect(frameHints(sharp, sharp).duplicate).toBe(true);
    expect(h.analysedAtEdge).toBe(false); // 64 px: below the calibrated edge, and it says so
  });

  it('a soft gradient is blurred even when bright', () => {
    const h = frameHints(frameStats(image(64, 48, (x) => 100 + x)));
    expect(h.dark).toBe(false);
    expect(h.blurred).toBe(true);
  });
});

describe('the hint scheduler is bounded and adaptive', () => {
  it('never queues: a frame offered while one is in flight is dropped', () => {
    let t = 0;
    const s = new HintScheduler({ now: () => t });
    expect(s.shouldSample()).toBe(true);
    s.started();
    t += 1000;
    expect(s.shouldSample()).toBe(false);
    expect(s.stats().dropped).toBe(1);
    s.finished();
    expect(s.stats().analysed).toBe(1);
  });

  it('spaces samples by the measured cost, within bounds', () => {
    let t = 0;
    const s = new HintScheduler({ now: () => t });
    s.started(); t += 300; s.finished();
    expect(s.stats().intervalMs).toBe(1200);
    s.started(); t += 5000; s.finished();
    expect(s.stats().intervalMs).toBe(MAX_INTERVAL_MS);
    s.started(); t += 1; s.finished();
    expect(s.stats().intervalMs).toBe(MIN_INTERVAL_MS);
    expect(s.shouldSample()).toBe(false); // within the interval
  });
});

describe('camera support is feature-detected', () => {
  it('no mediaDevices, or an insecure page, means no camera', () => {
    expect(cameraSupport({ navigator: {} }).camera).toBe(false);
    const insecure = cameraSupport({ isSecureContext: false, navigator: { mediaDevices: { getUserMedia() {} } } });
    expect(insecure.camera).toBe(false);
    expect(insecure.insecure).toBe(true);
  });

  it('picks the first recording type the browser supports', () => {
    class Rec { static isTypeSupported(t) { return t === 'video/webm'; } }
    const s = cameraSupport({ navigator: { mediaDevices: { getUserMedia() {} } }, MediaRecorder: Rec, Worker() {} });
    expect(s).toMatchObject({ camera: true, recording: true, recordingType: 'video/webm', worker: true });
  });

  it('turns camera errors into a state and a sentence, never raw text', () => {
    expect(cameraErrorState({ name: 'NotAllowedError', message: 'secret detail' })).toMatchObject({ state: 'denied' });
    expect(cameraErrorState({ name: 'NotFoundError' }).state).toBe('no_camera');
    expect(cameraErrorState({ name: 'NotReadableError' }).state).toBe('in_use');
    expect(cameraErrorState(new Error('boom')).message).not.toMatch(/boom/);
  });
});

describe('capture originals are staged until acknowledged', () => {
  const blob = (n) => new Blob([new Uint8Array(n)], { type: 'video/webm' });

  it('stages per property and lists oldest first', async () => {
    const store = createCaptureStore({ adapter: memoryAdapter(), estimate: async () => ({ quota: 1e9, usage: 0 }) });
    const a = await store.stage(blob(10), { subject: 'lead_id=1', name: 'a.webm', kind: 'video' });
    await store.stage(blob(10), { subject: 'lead_id=2', name: 'b.webm', kind: 'video' });
    const rows = await store.pending('lead_id=1');
    expect(rows.map((r) => r.name)).toEqual(['a.webm']);
    await store.remove(a.id);
    expect(await store.pending('lead_id=1')).toEqual([]);
  });

  it('refuses honestly when there is no room or no storage', async () => {
    const full = createCaptureStore({ adapter: memoryAdapter(), estimate: async () => ({ quota: 100, usage: 95 }) });
    await expect(full.stage(blob(10), { subject: 's', name: 'x', kind: 'video' })).rejects.toMatchObject({ reason: 'quota' });
    const none = createCaptureStore({ adapter: null });
    await expect(none.stage(blob(1), { subject: 's', name: 'x', kind: 'video' })).rejects.toBeInstanceOf(StorageRefused);
    expect(await none.pending('s')).toEqual([]);
  });
});
