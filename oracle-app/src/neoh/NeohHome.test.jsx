// @vitest-environment jsdom
/**
 * Home: what matters today, and the first useful thing to do about it.
 */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { crmGet } from '../state/useCrmApi';
import { NeohHome } from './NeohHome';

vi.mock('../state/useCrmApi', () => ({ crmGet: vi.fn() }));
vi.mock('../components/IntelligenceFeed', () => ({
  ConfidenceMeter: () => null, DecisionBar: () => null, EvidenceList: () => null,
}));
vi.mock('../components/AssistantContext', () => ({
  useOptionalAssistant: () => globalThis.__assistant,
}));

const CLIENT_ID = 'cccccccc-2222-4222-8222-222222222222';

function briefing(overrides = {}) {
  return {
    changed: { handled_automatically: 3, handled_breakdown: { add_client_note: 2, update_listing: 1 }, new_clients: 0 },
    attention: {
      opportunities: [{
        kind: 'next_best_action', subject: 'Sarah Johnson', subject_id: CLIENT_ID, subject_type: 'client',
        headline: 'Asked about a second showing', recommended_action: 'Call her back today', confidence: 0.8,
      }],
    },
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal('requestAnimationFrame', (callback) => setTimeout(callback, 0));
  vi.stubGlobal('cancelAnimationFrame', clearTimeout);
  globalThis.__assistant = { registerRecord: vi.fn(), requestCommand: vi.fn() };
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('NeohHome', () => {
  it('makes asking Neoh about the top person the obvious first action', async () => {
    crmGet.mockResolvedValue(briefing());
    const onNavigate = vi.fn();
    render(<NeohHome onNavigate={onNavigate} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Ask Neoh about Sarah Johnson' }));
    expect(globalThis.__assistant.registerRecord).toHaveBeenCalledWith(
      { type: 'client', id: CLIENT_ID, label: 'Sarah Johnson' }, 'home',
    );
    expect(globalThis.__assistant.requestCommand).toHaveBeenCalledWith(
      expect.objectContaining({ surface: 'conversation', rawText: expect.stringMatching(/Sarah Johnson/) }),
    );
    expect(onNavigate).toHaveBeenCalledWith('neoh');
  });

  it('opens the property a card is about when its subject is pressed', async () => {
    const LEAD_ID = 'aaaaaaaa-1111-4111-8111-111111111111';
    crmGet.mockResolvedValue(briefing({
      attention: {
        opportunities: [{
          kind: 'listing_buyer_match', subject: '123 Main Street, Wilmington', subject_id: LEAD_ID,
          subject_type: 'lead', headline: 'May fit Sarah Johnson',
          recommended_action: 'Ask Neoh about Sarah and this home, then reach out.', confidence: 0.75,
        }],
      },
    }));
    const onOpenEntity = vi.fn();
    render(<NeohHome onNavigate={vi.fn()} onOpenEntity={onOpenEntity} />);
    fireEvent.click(await screen.findByRole('button', { name: '123 Main Street, Wilmington' }));
    expect(onOpenEntity).toHaveBeenCalledWith(`/property/${LEAD_ID}`);
  });

  it('says what Neoh handled in product verbs, never tool names', async () => {
    crmGet.mockResolvedValue(briefing());
    render(<NeohHome onNavigate={vi.fn()} />);
    const toggle = await screen.findByRole('button', { name: /Neoh handled 3/ });
    if (toggle.getAttribute('aria-expanded') !== 'true') fireEvent.click(toggle);
    expect(screen.getByText('notes added')).toBeTruthy();
    expect(screen.getByText('listing updated')).toBeTruthy();
    expect(screen.queryByText(/update listing|add client note/)).toBeNull();
  });

  it('gives an empty CRM one clear next action', async () => {
    crmGet.mockImplementation((path) => (
      path === '/api/command-center'
        ? Promise.resolve(briefing({ attention: { opportunities: [] }, changed: {} }))
        : Promise.resolve({ results: [] })
    ));
    const onNavigate = vi.fn();
    render(<NeohHome onNavigate={onNavigate} />);
    expect(await screen.findByText('No contacts yet. Import them or add your first client.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Add or import contacts' }));
    expect(onNavigate).toHaveBeenCalledWith('people');
  });

  it('on a quiet day with data, offers a real record to ask about', async () => {
    crmGet.mockImplementation((path) => (
      path === '/api/command-center'
        ? Promise.resolve(briefing({ attention: { opportunities: [] }, changed: {} }))
        : Promise.resolve({ results: [{ kind: 'properties', id: CLIENT_ID, label: '12 Main St' }] })
    ));
    render(<NeohHome onNavigate={vi.fn()} />);
    expect(await screen.findByRole('button', { name: 'Ask Neoh about 12 Main St' })).toBeTruthy();
  });

  it('a busy Home makes exactly one request', async () => {
    crmGet.mockResolvedValue(briefing());
    render(<NeohHome onNavigate={vi.fn()} />);
    await screen.findByText('Sarah Johnson');
    expect(crmGet.mock.calls.map(([path]) => path)).toEqual(['/api/command-center']);
  });

  it('fails in product language and offers to try again', async () => {
    crmGet.mockRejectedValueOnce(new Error('HTTP 503')).mockResolvedValueOnce(briefing());
    render(<NeohHome onNavigate={vi.fn()} />);
    expect(await screen.findByText(/briefing didn.t load/)).toBeTruthy();
    expect(screen.queryByText(/503/)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(screen.getByText('Sarah Johnson')).toBeTruthy());
  });
});
