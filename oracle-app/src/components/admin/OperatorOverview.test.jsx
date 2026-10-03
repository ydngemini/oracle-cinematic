// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmGet: vi.fn() }));
vi.mock('../../state/useCrmApi', () => api);

const { default: OperatorOverview } = await import('./OperatorOverview');
const model = await import('./operatorModel');

const T1 = '11111111-1111-1111-1111-111111111111';
const T2 = '22222222-2222-2222-2222-222222222222';

const caps = (over = {}) => ({
  phone: { state: 'READY', reason: 'Business number verified for calls' },
  messaging: { state: 'READY', reason: 'Texting active' },
  email: { state: 'NOT_USED', reason: 'No mailbox connected' },
  calendar: { state: 'NOT_USED', reason: 'No calendar connected' },
  mls: { state: 'READY', reason: 'Licensed MLS feed synced and fresh' },
  billing: { state: 'READY', reason: 'Subscription active' },
  ...over,
});

const FIXTURES = {
  '/api/admin/health/components': {
    state: 'DEGRADED',
    components: {
      database: { state: 'HEALTHY', summary: 'database answering' },
      workers: { state: 'UNAVAILABLE', summary: 'no background worker alive' },
      job_queue: { state: 'HEALTHY', summary: 'queue moving' },
      mls: { state: 'STALE', summary: '1 MLS feed(s) stale' },
    },
    open_alerts: [{ component: 'workers', summary: 'no background worker alive', opened_at: new Date().toISOString() }],
  },
  '/api/admin/release': {
    api: { git_sha: 'abcdef1234567', environment: 'production' },
    migration_head: '0124_job_claim_aging.sql',
    workers: { healthy: true, live: 1, live_git_shas: ['0ld0000aaaa'] },
    warnings: [{ code: 'worker_build_differs', message: 'Workers are not running the same build as this API' }],
  },
  '/api/admin/brokerages?limit=100': {
    brokerages: [
      {
        id: T2, name: 'Brand New Homes', status: 'onboarding', agents: { total: 1, active: 1 },
        capabilities: caps({ phone: { state: 'NEEDS_SETUP', reason: 'No business number connected' },
                             billing: { state: 'NEEDS_SETUP', reason: 'No subscription' } }),
        needs_action: [{ code: 'billing_none', message: 'No subscription' },
                       { code: 'setup_incomplete', message: 'Setup incomplete: phone, billing' }],
        work: { failed_jobs_24h: 2, unresolved_side_effects: 1 },
      },
      {
        id: T1, name: 'Lockwood Realty', status: 'live', agents: { total: 4, active: 3 },
        capabilities: caps(), needs_action: [], work: { failed_jobs_24h: 0, unresolved_side_effects: 0 },
      },
    ],
    summary: { live: 1, onboarding: 1, needs_action: 1 },
    page: { limit: 100, offset: 0, total: 2, has_more: false },
    not_tracked: { usage_limits: '…' },
  },
  '/api/admin/billing/exceptions': {
    no_subscription: [{ tenant_id: T2, name: 'Brand New Homes', status: 'none' }],
    payment_issue: [], canceled: [], reactivated_90d: [],
    webhooks: { stripe_mode: 'live', webhook_secret_configured: true, last_processed_at: null,
                refused_since_start: { invalid_signature: 3 } },
    usage_metering: { state: 'off', unreported: 12 },
  },
  '/api/admin/mls/feeds': {
    feeds: [{ mls_id: 'actris', mls_name: 'ACTRIS', licensed: true, health: 'STALE' }],
  },
  '/api/admin/comms': { brokerages: [] },
  '/api/admin/ai': {
    gateway: { configured: true, current: { analysis: { provider: 'fireworks', model: 'kimi' } },
               providers: { fireworks: { calls: 10, failures: 0, rate_limited: 2, failure_rate: 0 } } },
    health: { state: 'HEALTHY', summary: 'Neoh responding' },
    chat_24h: { latency_seconds: { p50: 1.2, p95: 4.5, n: 10 } },
    tools_24h: { total: 10, failed: 0, failure_rate: 0 },
  },
  '/api/admin/anomalies?limit=20': {
    alerts: [{ id: 'a1', anomaly_type: 'cross_tenant_probe', severity: 'critical', created_at: new Date().toISOString() },
             { id: 'a2', anomaly_type: 'login_burst', severity: 'low', created_at: new Date().toISOString() }],
  },
  '/api/admin/pilot-metrics?days=7': {
    window_days: 7, deal_window_days: 90,
    total: {
      active_agents: 3, neoh_conversation_turns: 40, neoh_completed_actions: 19,
      outreach_through_neoh: { calls: 2, texts: 5, emails: 1, calendar: 0 }, matches_acted_on: 0,
      deal_activity: { closed: 4, associated: 1, associated_deal_value: 450000 },
    },
  },
};

const DIAG = {
  tenant: { id: T2, name: 'Brand New Homes', lifecycle_state: 'active' },
  plan: { status: 'none' },
  agents: { total: 1, active: 1, owners: 1 },
  capabilities: caps({ phone: { state: 'NEEDS_SETUP', reason: 'No business number connected' } }),
  needs_action: [{ code: 'billing_none', message: 'No subscription' }],
  voice: { routes: [], inbound_calls_24h: { total: 0, failed: 0 } },
  messaging: { routes: [], brand_10dlc: { status: 'failed', failure_reason: 'EIN [number] mismatch' }, campaigns_10dlc: [] },
  email_calendar: { connections: [], self_reported_status: 'NOT_STARTED' },
  mls: [],
  jobs: { groups: [{ job_type: 'outreach.sms', state: 'dead_letter', error_code: 'provider_timeout', count: 2, last_at: null }] },
  side_effects: { groups: [] },
  provider_errors: { ai_responses: [], ai_tools: [], sms: [], email_failed: 0 },
  recent_audit: [{ category: 'admin_action', action: 'role_change', at: null }],
  release: { api: { git_sha: 'abcdef1234567' } },
  excluded: ['message bodies', 'tokens and credentials'],
  unavailable: [],
};

function respond(overrides = {}) {
  api.crmGet.mockImplementation((path) => {
    if (path in overrides) {
      const v = overrides[path];
      return v instanceof Error ? Promise.reject(v) : Promise.resolve(v);
    }
    if (path.includes('/diagnostics')) return Promise.resolve(DIAG);
    if (path in FIXTURES) return Promise.resolve(FIXTURES[path]);
    return Promise.reject(new Error(`unexpected ${path}`));
  });
}

beforeEach(() => { api.crmGet.mockReset(); respond(); });
afterEach(cleanup);

describe('OperatorOverview', () => {
  it('leads with production health in words, not colour alone', async () => {
    render(<OperatorOverview />);
    const card = (await screen.findByRole('heading', { name: 'Production health' })).closest('section');
    await within(card).findByText('Background workers: Unavailable');
    expect(within(card).getByText('Degraded')).toBeTruthy();
    expect(within(card).getByText('MLS feeds: Stale')).toBeTruthy();
    expect(within(card).getByText(/Open alerts \(1\)/)).toBeTruthy();
    expect(screen.getByText(/Production degraded/)).toBeTruthy();
  });

  it('shows the release and warns when workers run another build', async () => {
    render(<OperatorOverview />);
    expect(await screen.findByText('Workers are not running the same build as this API')).toBeTruthy();
    expect(screen.getByText('abcdef1')).toBeTruthy();
    expect(screen.getByText(/1 live on 0ld0000/)).toBeTruthy();
  });

  it('lists the brokerages that need action with what they need', async () => {
    render(<OperatorOverview />);
    const card = (await screen.findByRole('heading', { name: 'Brokerages' })).closest('section');
    await within(card).findByText('Brand New Homes');
    expect(within(card).getByText('Setup incomplete: phone, billing')).toBeTruthy();
    expect(within(card).getByText('Phone: Needs setup')).toBeTruthy();
    // A brokerage with nothing to do is folded away until asked for.
    expect(within(card).queryByText('Lockwood Realty')).toBeNull();
    fireEvent.click(within(card).getByRole('button', { name: /Show 1 brokerage without issues/ }));
    expect(within(card).getByText('Lockwood Realty')).toBeTruthy();
    expect(screen.getByText(/1 brokerage needs action/)).toBeTruthy();
  });

  it('opens the diagnostics bundle for one brokerage and moves focus to it', async () => {
    render(<OperatorOverview />);
    const button = await screen.findByRole('button', { name: 'Diagnostics' });
    fireEvent.click(button);
    const heading = await screen.findByRole('heading', { name: 'Diagnostics for Brand New Homes' });
    expect(button.getAttribute('aria-expanded')).toBe('true');
    await waitFor(() => expect(document.activeElement).toBe(heading));
    expect(api.crmGet).toHaveBeenCalledWith(`/api/admin/brokerages/${T2}/diagnostics`);
    expect(screen.getByText('outreach.sms · dead_letter × 2')).toBeTruthy();
    expect(screen.getByText(/Brand: failed — EIN \[number\] mismatch/)).toBeTruthy();
    expect(screen.getByText(/Never included: message bodies/)).toBeTruthy();
  });

  it('degrades one card when its endpoint fails and keeps the rest', async () => {
    const err = Object.assign(new Error('boom'), { status: 500 });
    respond({ '/api/admin/billing/exceptions': err, '/api/admin/release': err });
    render(<OperatorOverview />);
    expect(await screen.findByText('Couldn’t load billing exceptions.')).toBeTruthy();
    expect(screen.getByText('Couldn’t load release.')).toBeTruthy();
    expect(await screen.findByText('Setup incomplete: phone, billing')).toBeTruthy();
    expect(screen.getAllByRole('alert').length).toBe(2);
  });

  it('surfaces provider degradation, security and billing exceptions', async () => {
    render(<OperatorOverview />);
    expect(await screen.findByText('MLS ACTRIS: stale')).toBeTruthy();
    expect(screen.getByText(/fireworks is rate limiting/)).toBeTruthy();
    expect(screen.getByText('critical: cross tenant probe')).toBeTruthy();
    expect(screen.queryByText(/login burst/)).toBeNull();
    expect(screen.getByText(/invalid signature ×3/)).toBeTruthy();
    expect(screen.getByText('off (history only)')).toBeTruthy();
  });

  it('keeps pilot metrics small and labels attribution honestly', async () => {
    render(<OperatorOverview />);
    const card = (await screen.findByRole('heading', { name: /Pilot metrics/ })).closest('section');
    await within(card).findByText('Active agents');
    expect(card.querySelectorAll('dt').length).toBeLessThanOrEqual(8);
    expect(within(card).getByText(/1 of 4 · \$450,000 deal value/)).toBeTruthy();
    expect(within(card).getByText(/not proof it caused it/)).toBeTruthy();
  });
});

describe('operatorModel', () => {
  it('maps every state to a tone with a distinct glyph', () => {
    const glyphs = new Set(['ok', 'warn', 'bad', 'muted'].map(model.toneGlyph));
    expect(glyphs.size).toBe(4);
    expect(model.toneOf('READY')).toBe('ok');
    expect(model.toneOf('NEEDS_ATTENTION')).toBe('bad');
    expect(model.toneOf('SOMETHING_NEW')).toBe('warn');
  });

  it('lists unhealthy components worst first', () => {
    const out = model.componentProblems(FIXTURES['/api/admin/health/components']);
    expect(out.map((p) => p.name)).toEqual(['workers', 'mls']);
  });

  it('reports no degradation for a healthy fleet', () => {
    expect(model.providerDegradation({
      ai: { health: { state: 'HEALTHY' }, gateway: { configured: true, providers: {} }, tools_24h: {} },
      mls: { feeds: [{ mls_id: 'x', mls_name: 'X', licensed: false, health: 'STALE' }] },
      comms: { brokerages: [{ name: 'A', problems: [] }] },
    })).toEqual([]);
  });
});
