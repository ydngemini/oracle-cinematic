// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmGet: vi.fn() }));
vi.mock('../state/useCrmApi', () => api);

const { default: AssistantCapabilities } = await import('./AssistantCapabilities');
const { groupAreas } = await import('../lib/capabilityOrder');

afterEach(() => { cleanup(); vi.clearAllMocks(); });

const BODY = {
  version: 'test',
  note: 'Derived from the tools the assistant can actually run.',
  not_offered: ['MLS, IDX or RESO listing search'],
  areas: [
    { id: 'public_records', label: 'Public records', summary: 'Flood zone and liens.', state: 'unavailable', state_label: 'Not available yet', limits: [], role_limited: false },
    { id: 'outreach', label: 'Email, text, call and calendar drafts', summary: 'Nothing is sent until you approve it.', state: 'approval_gated', state_label: 'Needs your approval', limits: [], role_limited: false },
    { id: 'clients', label: 'Client lookups', summary: 'Find clients.', state: 'available', state_label: 'Available', limits: [], role_limited: false },
    { id: 'market', label: 'Market figures', summary: 'County and state.', state: 'degraded', state_label: 'Limited', limits: ['county and state level only'], role_limited: false },
    { id: 'workspace_admin', label: 'Workspace health and billing (owner only)', summary: 'For the broker-owner.', state: 'unavailable', state_label: 'Not available yet', limits: [], role_limited: true },
  ],
};

describe('AssistantCapabilities', () => {
  it('shows each area with the state the API derived, usable ones first', async () => {
    api.crmGet.mockResolvedValue(BODY);
    render(<AssistantCapabilities />);
    expect(api.crmGet).toHaveBeenCalledWith('/api/ai/chat/capabilities');
    expect(await screen.findByText('3 of 5 usable')).toBeTruthy();
    const labels = screen.getAllByRole('listitem').map((item) => item.querySelector('strong').textContent);
    expect(labels[0]).toBe('Client lookups');
    expect(labels.at(-1)).toMatch(/Public records|Workspace health/);
    expect(screen.getByText('Needs your approval')).toBeTruthy();
    expect(screen.getByText(/Limited: county and state level only/)).toBeTruthy();
    expect(screen.getByText('Not available yet')).toBeTruthy();
    expect(screen.getByText(/Not offered: MLS/)).toBeTruthy();
  });

  it('labels a role gate as broker-owner only, never "not available yet"', async () => {
    api.crmGet.mockResolvedValue(BODY);
    render(<AssistantCapabilities />);
    const row = (await screen.findByText('Workspace health and billing (owner only)')).closest('li');
    expect(row.textContent).toContain('Broker-owner only');
    expect(row.textContent).not.toContain('Not available yet');
  });

  it('claims nothing when the registry cannot be loaded', async () => {
    api.crmGet.mockRejectedValue(Object.assign(new Error('down'), { status: 503 }));
    render(<AssistantCapabilities />);
    expect(await screen.findByText(/Nothing below is assumed to work/)).toBeTruthy();
    expect(screen.queryAllByRole('listitem')).toHaveLength(0);
  });

  it('orders by state and tolerates a missing list', () => {
    expect(groupAreas(undefined)).toEqual([]);
    expect(groupAreas(BODY.areas).map((a) => a.state)).toEqual(
      ['available', 'degraded', 'approval_gated', 'unavailable', 'unavailable'],
    );
  });
});
