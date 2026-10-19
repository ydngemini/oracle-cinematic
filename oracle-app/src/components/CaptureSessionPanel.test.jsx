// @vitest-environment jsdom
/**
 * Neoh Space capture panel: a normal person can start a build, watch it by
 * stage, come back to it, and recover from a failure — without ever seeing a
 * fake percentage or a toolchain word.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

afterEach(cleanup);

const crmGet = vi.fn();
const crmPost = vi.fn();
vi.mock('../state/useCrmApi', () => ({
  crmGet: (...a) => crmGet(...a),
  crmPost: (...a) => crmPost(...a),
}));

const { default: CaptureSessionPanel } = await import('./CaptureSessionPanel');
const { CAPTURE_STEPS } = await import('../lib/tour/captureGuide');

const LEAD = '33333333-3333-4333-8333-333333333333';

const steps = (current) => ['queued', 'preparing', 'reconstructing', 'converting', 'analyzing', 'ready']
  .map((stage, i, all) => ({
    stage,
    label: stage,
    state: i < all.indexOf(current) ? 'done' : stage === current ? 'current' : 'pending',
  }));

const job = (over = {}) => ({
  job_id: 'j1', state: 'reconstructing', label: 'Building your space',
  message: 'This is the long step.', active: true, terminal: false,
  steps: steps('reconstructing'), guidance: [], caveats: [], can_retry: false,
  retry_kind: null, ...over,
});

function apiError(status, message) {
  const err = new Error(message);
  err.status = status;
  return err;
}

beforeEach(() => {
  vi.clearAllMocks();
  crmGet.mockResolvedValue({ published: null, latest_build: null });
});

describe('capture panel', () => {
  it('explains how to capture in plain language', () => {
    render(<CaptureSessionPanel leadId={LEAD} photoCount={30} />);
    fireEvent.click(screen.getByRole('button', { name: /how to capture/i }));
    const guide = screen.getByRole('list');
    expect(guide.textContent).toMatch(/Walk slowly/);
    expect(CAPTURE_STEPS.map((s) => s.title)).toEqual(
      ['Start', 'Move through the property', 'Cover everything', 'Finish', 'Upload']);
    for (const word of ['COLMAP', 'Gaussian', 'splat', 'solver', 'GPU', 'reconstruction']) {
      expect(document.body.textContent).not.toMatch(new RegExp(word, 'i'));
    }
  });

  it('resumes watching a build that was running when the page was left', async () => {
    crmGet.mockImplementation((url) => (url.startsWith('/api/crm/space')
      ? Promise.resolve({ published: null, latest_build: job() })
      : Promise.resolve(job())));
    render(<CaptureSessionPanel leadId={LEAD} photoCount={30} />);
    expect(await screen.findByText('Building your space')).toBeTruthy();
    const current = screen.getByRole('list', { name: /build steps/i })
      .querySelector('[aria-current="step"]');
    expect(current.textContent).toBe('reconstructing');
    // Never a percentage — no provider reports a real one.
    expect(document.body.textContent).not.toMatch(/\d+\s*%/);
    expect(screen.queryByRole('progressbar')).toBeNull();
  });

  it('sends an idempotency key so a double tap is one build', async () => {
    crmPost.mockResolvedValue(job({ state: 'queued', label: 'Waiting to start' }));
    render(<CaptureSessionPanel leadId={LEAD} photoCount={30} />);
    fireEvent.click(await screen.findByRole('button', { name: /build 3d space/i }));
    await waitFor(() => expect(crmPost).toHaveBeenCalled());
    expect(crmPost.mock.calls[0][0]).toMatch(/idempotency_key=[^&]+/);
    expect(crmPost.mock.calls[0][0]).not.toMatch(/confirm_rebuild/);
  });

  it('asks before replacing a published space, and says the old one stays', async () => {
    crmPost
      .mockRejectedValueOnce(apiError(409, 'This property already has a 3D space. Confirm to rebuild it — the current space stays visible until the new one is ready.'))
      .mockResolvedValueOnce(job({ state: 'queued' }));
    render(<CaptureSessionPanel leadId={LEAD} photoCount={30} />);
    fireEvent.click(await screen.findByRole('button', { name: /build 3d space/i }));
    const confirm = await screen.findByRole('button', { name: /keep the current space/i });
    expect(screen.getByText(/stays visible/)).toBeTruthy();
    fireEvent.click(confirm);
    await waitFor(() => expect(crmPost).toHaveBeenCalledTimes(2));
    expect(crmPost.mock.calls[1][0]).toMatch(/confirm_rebuild=true/);
  });

  it('shows recapture guidance when the capture was refused', async () => {
    crmGet.mockImplementation((url) => Promise.resolve(url.startsWith('/api/crm/space') ? {
      published: null,
      latest_build: job({
        state: 'failed', active: false, terminal: true, label: "Couldn't build this space",
        message: "This capture can't make a usable space yet.",
        guidance: ['Walk slower and hold the phone steady; avoid quick turns.'],
        can_retry: true, retry_kind: 'recapture', steps: [],
      }),
    } : null));
    render(<CaptureSessionPanel leadId={LEAD} photoCount={30} />);
    expect(await screen.findByText(/Walk slower/)).toBeTruthy();
    expect(screen.getByRole('button', { name: /build again after uploading more/i })).toBeTruthy();
  });

  it('retries a failed packaging step without rebuilding', async () => {
    crmGet.mockImplementation((url) => Promise.resolve(url.startsWith('/api/crm/space') ? {
      published: null,
      latest_build: job({ state: 'needs_attention', active: false, terminal: true,
        can_retry: true, retry_kind: 'conversion', steps: [] }),
    } : job()));
    crmPost.mockResolvedValue(job({ state: 'queued' }));
    render(<CaptureSessionPanel leadId={LEAD} photoCount={30} />);
    fireEvent.click(await screen.findByRole('button', { name: /finish preparing the space/i }));
    await waitFor(() => expect(crmPost).toHaveBeenCalled());
    expect(crmPost.mock.calls[0][0]).toBe('/api/crm/reconstruction-jobs/j1/retry');
  });

  it('states caveats of a ready space instead of hiding them', async () => {
    crmGet.mockImplementation((url) => Promise.resolve(url.startsWith('/api/crm/space') ? {
      published: { media_id: 'm' }, latest_build: job() } : job({
      state: 'ready', active: false, terminal: true, label: 'Ready',
      caveats: ['Measurements unavailable — real-world scale unknown'], floorplan: 'unavailable',
      steps: steps('ready'),
    })));
    render(<CaptureSessionPanel leadId={LEAD} photoCount={30} />);
    expect(await screen.findByText(/scale unknown/)).toBeTruthy();
    expect(screen.getByText(/floor plan could not be made/i)).toBeTruthy();
  });

  it('keeps "ready to view" and the quality check visibly separate (T12.09)', async () => {
    const certification = {
      state: 'incomplete',
      label: 'Quality checks incomplete',
      reasons: ["It hasn't been tested on real phones yet", 'Some views may look soft or blurry'],
    };
    crmGet.mockImplementation((url) => Promise.resolve(url.startsWith('/api/crm/space') ? {
      published: { media_id: 'm' }, latest_build: job() } : job({
      state: 'ready', active: false, terminal: true, label: 'Ready',
      caveats: ['Some views may look soft or blurry'], steps: steps('ready'), certification,
    })));
    render(<CaptureSessionPanel leadId={LEAD} photoCount={30} />);
    const quality = await screen.findByRole('status', { name: /quality check/i });
    expect(quality.textContent).toMatch(/Quality checks incomplete/);
    expect(quality.textContent).toMatch(/real phones/);
    // Said once: a caveat already listed is not repeated as a reason.
    expect(screen.getAllByText('Some views may look soft or blurry')).toHaveLength(1);
    expect(screen.getByText(/your space is ready/i)).toBeTruthy();
    expect(document.body.textContent.replace(/not verified/gi, '')).not.toMatch(/verified|professional|full 3d/i);
  });

  it('shows an operator each check and its status', async () => {
    const certification = {
      state: 'incomplete',
      label: 'Quality checks incomplete',
      reasons: [],
      checks: {
        held_out_quality: { label: 'Image quality', status: 'passed' },
        registration: { label: 'Camera placement', status: 'passed' },
        room_connectivity: { label: 'Room connections', status: 'missing' },
        device_performance: { label: 'Phone performance', status: 'missing' },
      },
    };
    crmGet.mockImplementation((url) => Promise.resolve(url.startsWith('/api/crm/space') ? {
      published: { media_id: 'm' }, latest_build: job() } : job({
      state: 'ready', active: false, terminal: true, label: 'Ready', steps: steps('ready'), certification,
    })));
    render(<CaptureSessionPanel leadId={LEAD} photoCount={30} />);
    const checks = await screen.findByRole('list', { name: /quality checks/i });
    expect(checks.textContent).toMatch(/Room connections: not measured/);
    expect(checks.textContent).toMatch(/Image quality: passed/);
  });

  it('once built, shows the published space\'s quality check, not the build view\'s', async () => {
    // The build view never compares the build with the property's current
    // photos; the published space does. So the panel re-reads it when ready.
    const fromBuild = { state: 'incomplete', label: 'Quality checks incomplete',
      reasons: ['from the build view'] };
    const fromSpace = {
      state: 'pending', label: 'Quality not verified yet',
      reasons: ["Photos or video were added after this space was built; it hasn't been checked against them"],
      checks: { registration: { label: 'Camera placement', status: 'passed' } },
    };
    let summaries = 0;
    crmGet.mockImplementation((url) => {
      if (url.startsWith('/api/crm/space')) {
        summaries += 1;
        return Promise.resolve(summaries === 1
          ? { published: null, latest_build: job() }
          : { published: { media_id: 'm1', certification: fromSpace }, latest_build: null });
      }
      return Promise.resolve(job({
        state: 'ready', active: false, terminal: true, label: 'Ready', media_id: 'm1',
        steps: steps('ready'), certification: fromBuild,
      }));
    });
    render(<CaptureSessionPanel leadId={LEAD} photoCount={30} />);
    await screen.findByText(/your space is ready/i);
    await waitFor(() => expect(screen.getByRole('status', { name: /quality check/i }).textContent)
      .toMatch(/Quality not verified yet/));
    const quality = screen.getByRole('status', { name: /quality check/i });
    expect(quality.textContent).toMatch(/added after this space was built/);
    expect(quality.textContent).not.toMatch(/from the build view/);
    expect(screen.getByRole('list', { name: /quality checks/i }).textContent).toMatch(/Camera placement: passed/);
    expect(summaries).toBe(2);
  });
});
