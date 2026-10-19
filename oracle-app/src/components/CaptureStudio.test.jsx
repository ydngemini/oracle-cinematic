// @vitest-environment jsdom
/**
 * Capture Studio (T12.03, T12.05; L12-03): no camera is never a dead end,
 * a recording is kept on the device until the server acknowledged it, and a
 * failed upload loses nothing.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import CaptureStudio from './CaptureStudio';
import { createCaptureStore, memoryAdapter } from '../lib/capture/captureStore';

afterEach(cleanup);

const roomy = async () => ({ quota: 1e9, usage: 0 });

function cameraEnv({ getUserMedia, recorder = true } = {}) {
  const track = { stop: vi.fn() };
  const stream = { getTracks: () => [track] };
  class FakeRecorder {
    static isTypeSupported(t) { return t === 'video/webm'; }
    constructor() { this.state = 'inactive'; FakeRecorder.last = this; }
    start() { this.state = 'recording'; }
    stop() {
      this.state = 'inactive';
      this.ondataavailable?.({ data: new Blob([new Uint8Array(2048)], { type: 'video/webm' }) });
      this.onstop?.();
    }
  }
  return {
    env: {
      navigator: { mediaDevices: { getUserMedia: getUserMedia || vi.fn(async () => stream) } },
      MediaRecorder: recorder ? FakeRecorder : undefined,
      document,
      setInterval: () => 1,
      clearInterval: () => {},
    },
    track,
    FakeRecorder,
  };
}

describe('no camera is never a dead end', () => {
  it('an unsupported browser offers the file picker straight away', () => {
    const onChooseFiles = vi.fn();
    render(<CaptureStudio subject="lead_id=1" env={{ navigator: {}, document }} onChooseFiles={onChooseFiles}
      onUpload={vi.fn()} store={createCaptureStore({ adapter: memoryAdapter() })} />);
    expect(screen.getByRole('alert').textContent).toMatch(/cannot use the camera/);
    fireEvent.click(screen.getByRole('button', { name: 'Choose files instead' }));
    expect(onChooseFiles).toHaveBeenCalled();
  });

  it('a refused permission says how to allow it and offers files', async () => {
    const denied = Object.assign(new Error('Permission denied by user agent detail'), { name: 'NotAllowedError' });
    const { env } = cameraEnv({ getUserMedia: vi.fn(async () => { throw denied; }) });
    render(<CaptureStudio subject="lead_id=1" env={env} onChooseFiles={vi.fn()} onUpload={vi.fn()}
      store={createCaptureStore({ adapter: memoryAdapter() })} />);
    fireEvent.click(screen.getByRole('button', { name: 'Start camera' }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toMatch(/turned down/);
    expect(alert.textContent).not.toMatch(/user agent detail/);
    expect(screen.getByRole('button', { name: 'Choose files instead' })).toBeTruthy();
  });
});

describe('originals survive until the server acknowledges them', () => {
  it('a recording is staged on the device, uploaded, and dropped only when acknowledged', async () => {
    const adapter = memoryAdapter();
    const store = createCaptureStore({ adapter, estimate: roomy });
    const { env, track } = cameraEnv();
    const onUpload = vi.fn(async (files) => ({ uploaded: files.map((f) => f.name), failed: [] }));
    render(<CaptureStudio subject="lead_id=1" env={env} onChooseFiles={vi.fn()} onUpload={onUpload} store={store} />);

    fireEvent.click(screen.getByRole('button', { name: 'Start camera' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Record walkthrough' }));
    fireEvent.click(screen.getByRole('button', { name: 'Stop recording' }));
    await screen.findByText(/Waiting to upload \(1/);
    expect((await adapter.all()).length).toBe(1);
    expect(screen.getByText('saved on this device')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Upload 1' }));
    await screen.findByText('1 acknowledged by the server.');
    expect(onUpload.mock.calls[0][0][0].type).toBe('video/webm');
    expect(await adapter.all()).toEqual([]);

    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(track.stop).toHaveBeenCalled();
  });

  it('a failed upload keeps the copy, and a later visit finds it', async () => {
    const adapter = memoryAdapter();
    const store = createCaptureStore({ adapter, estimate: roomy });
    await store.stage(new Blob([new Uint8Array(10)], { type: 'video/webm' }), { subject: 'lead_id=1', name: 'walk.webm', kind: 'video' });
    await store.stage(new Blob([new Uint8Array(10)], { type: 'video/webm' }), { subject: 'lead_id=9', name: 'other.webm', kind: 'video' });
    const onUpload = vi.fn(async () => ({ uploaded: [], failed: ['walk.webm'] }));
    const { env } = cameraEnv();
    render(<CaptureStudio subject="lead_id=1" env={env} onChooseFiles={vi.fn()} onUpload={onUpload} store={store} />);

    await screen.findByText('saved on this device earlier');
    expect(screen.queryByText('other.webm')).toBeNull(); // another property's capture is not shown here
    fireEvent.click(screen.getByRole('button', { name: 'Upload 1' }));
    await screen.findByText(/0 of 1 acknowledged by the server\. 1 still saved here/);
    expect((await adapter.all()).length).toBe(2);
  });

  it('when the device cannot keep a copy, it says the capture lives in this page only', async () => {
    const store = createCaptureStore({ adapter: memoryAdapter(), estimate: async () => ({ quota: 100, usage: 100 }) });
    const { env } = cameraEnv();
    render(<CaptureStudio subject="lead_id=1" env={env} onChooseFiles={vi.fn()} onUpload={vi.fn()} store={store} />);
    fireEvent.click(screen.getByRole('button', { name: 'Start camera' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Record walkthrough' }));
    fireEvent.click(screen.getByRole('button', { name: 'Stop recording' }));
    await screen.findByText('in this page only');
    await waitFor(() => expect(screen.getByText(/Not enough free space/)).toBeTruthy());
  });

  it('never shows a percentage or says verified', async () => {
    const { env } = cameraEnv();
    const { container } = render(<CaptureStudio subject="lead_id=1" env={env} onChooseFiles={vi.fn()} onUpload={vi.fn()}
      store={createCaptureStore({ adapter: memoryAdapter() })} />);
    fireEvent.click(screen.getByRole('button', { name: 'Start camera' }));
    await screen.findByRole('button', { name: 'Record walkthrough' });
    expect(container.textContent).not.toMatch(/%|verified/i);
  });
});

describe('live hints come from the worker and are labelled as hints', () => {
  it('shows the worker\'s hint over the preview and in a polite live region', async () => {
    const { env } = cameraEnv();
    env.Worker = function Worker() {};
    const worker = { postMessage: vi.fn(), terminate: vi.fn() };
    render(<CaptureStudio subject="lead_id=1" env={env} createWorker={() => worker} onChooseFiles={vi.fn()}
      onUpload={vi.fn()} store={createCaptureStore({ adapter: memoryAdapter() })} />);
    fireEvent.click(screen.getByRole('button', { name: 'Start camera' }));
    await screen.findByRole('button', { name: 'Record walkthrough' });
    worker.onmessage({ data: { hints: { dark: false, blurred: true, duplicate: false } } });
    await waitFor(() => expect(screen.getAllByText(/Blurry — slow down/).length).toBe(2));
    expect(screen.getByText(/quick check on this phone only/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(worker.terminate).toHaveBeenCalled();
  });
});
