// @vitest-environment jsdom
/**
 * The live version, rollback and custom-domain checks of one site (T14.04).
 *
 * Pins: the live revision is named; someone who may publish can roll back to
 * the previous live version after confirming, and the request carries the
 * version they saw; someone who may not sees no rollback; a domain shows the
 * exact TXT record to add and a plain status, never "Live"; "Check DNS" asks
 * the server and an unreachable DNS says nothing changed.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmGet: vi.fn(), crmPost: vi.fn(), crmPut: vi.fn(), crmDelete: vi.fn() }));
vi.mock('../state/useCrmApi', () => api);

const { default: SitePublishPanel } = await import('./SitePublishPanel');

const SITE = 'site-1';
const REVISIONS = [
  { id: 'rev-2', revision: 2, created_at: '2026-10-17T10:00:00Z' },
  { id: 'rev-1', revision: 1, created_at: '2026-10-16T10:00:00Z' },
];
const DETAIL = {
  site: { id: SITE, status: 'published', published_revision_id: 'rev-2' },
  revisions: REVISIONS,
  can_publish: true,
  publication: { status: 'published', live_revision_id: 'rev-2', rollback_to_revision_id: 'rev-1' },
};
const CHALLENGE = { type: 'TXT', name: '_neoh-site-verify.homes.example.com', value: 'neoh-site-verify=abc123' };
const DOMAIN = {
  id: 'dom-1', hostname: 'homes.example.com', status: 'pending', verified_at: null,
  last_checked_at: null, last_outcome: null, challenge: CHALLENGE,
};

function routes(overrides = {}) {
  const table = {
    [`/api/sites/${SITE}`]: DETAIL,
    [`/api/sites/${SITE}/funnel?days=90`]: { breakdown: [] },
    [`/api/sites/${SITE}/collaborators`]: { collaborators: [] },
    [`/api/sites/${SITE}/domains`]: { domains: [DOMAIN], serving: { available: false, target: null } },
    ...overrides,
  };
  api.crmGet.mockImplementation((path) => (
    path in table && table[path] instanceof Error ? Promise.reject(table[path]) : Promise.resolve(table[path])
  ));
}

beforeEach(() => {
  Object.values(api).forEach((fn) => fn.mockReset());
  routes();
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('SitePublishPanel', () => {
  it('names the live version and rolls back to the previous one after confirming', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    api.crmPost.mockResolvedValue({ live_revision_id: 'rev-1', previous_live_revision_id: 'rev-2', already_live: false });
    render(<SitePublishPanel siteId={SITE} />);
    expect(await screen.findByText('Live: v2')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Roll back to v1' }));
    await waitFor(() => expect(api.crmPost).toHaveBeenCalledWith(`/api/sites/${SITE}/rollback`, {
      expected_live_revision_id: 'rev-2', to_revision_id: 'rev-1',
    }));
    expect(window.confirm).toHaveBeenCalled();
    expect(await screen.findByText('v1 is live again.')).toBeTruthy();
  });

  it('does nothing when the rollback is not confirmed', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<SitePublishPanel siteId={SITE} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Roll back to v1' }));
    expect(api.crmPost).not.toHaveBeenCalled();
  });

  it('offers no rollback or domain checks to someone who may not publish', async () => {
    routes({
      [`/api/sites/${SITE}`]: { ...DETAIL, can_publish: false },
      [`/api/sites/${SITE}/domains`]: Object.assign(new Error('nope'), { status: 404 }),
    });
    render(<SitePublishPanel siteId={SITE} />);
    expect(await screen.findByText('Live: v2')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Roll back/ })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Check DNS' })).toBeNull();
  });

  it('shows the exact TXT record to add and a plain status, never Live', async () => {
    render(<SitePublishPanel siteId={SITE} />);
    const domains = await screen.findByRole('list', { name: 'Domains' });
    expect(within(domains).getByText('homes.example.com')).toBeTruthy();
    expect(within(domains).getByText('Waiting for the DNS record')).toBeTruthy();
    expect(within(domains).getByText(CHALLENGE.name)).toBeTruthy();
    expect(within(domains).getByText(CHALLENGE.value)).toBeTruthy();
    expect(within(domains).queryByText(/live/i)).toBeNull();
  });

  it('checks DNS and says plainly that nothing changed when DNS cannot be reached', async () => {
    api.crmPost.mockResolvedValue({
      domain: DOMAIN, outcome: 'dns_unavailable', retryable: true, retry_after_seconds: 30,
      message: 'DNS could not be reached. Nothing changed — try again in a moment.',
    });
    render(<SitePublishPanel siteId={SITE} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Check DNS' }));
    await waitFor(() => expect(api.crmPost).toHaveBeenCalledWith(`/api/sites/${SITE}/domains/dom-1/verify`, {}));
    expect(await screen.findByText(/Nothing changed — try again/)).toBeTruthy();
    expect(screen.getByText('Waiting for the DNS record')).toBeTruthy();
  });

  it('shows ownership verified without claiming the site is served there', async () => {
    const verified = { ...DOMAIN, status: 'verified', verified_at: '2026-10-18T09:00:00Z' };
    api.crmPost.mockResolvedValue({
      domain: verified, outcome: 'verified', retryable: false,
      message: 'Ownership verified. Serving the site at this address is not set up on this platform yet.',
    });
    routes({ [`/api/sites/${SITE}/domains`]: { domains: [DOMAIN], serving: { available: false, target: null } } });
    render(<SitePublishPanel siteId={SITE} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Check DNS' }));
    expect(await screen.findByText('Ownership verified')).toBeTruthy();
    expect(screen.getByText(/not set up on this platform yet/)).toBeTruthy();
    expect(within(screen.getByRole('list', { name: 'Domains' })).queryByText(/live/i)).toBeNull();
  });
});
