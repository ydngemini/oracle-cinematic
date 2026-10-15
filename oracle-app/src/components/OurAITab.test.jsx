// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmGet: vi.fn() }));
vi.mock('../state/useCrmApi', () => api);
vi.mock('../neoh/useNeohChannel', () => ({ fetchChatStatus: () => Promise.resolve({ enabled: true }) }));
vi.mock('./AssistantContext', () => ({ useAssistant: () => ({ requestCommand: vi.fn(), setOpen: vi.fn() }) }));
// Heavy child workspaces are irrelevant to the source banner.
vi.mock('./PersonalAITab', () => ({ default: () => null }));
vi.mock('./SalesWorkspace', () => ({ default: () => null }));
vi.mock('./StudioTab', () => ({ default: () => null }));

const { default: OurAITab } = await import('./OurAITab');

afterEach(() => { cleanup(); vi.clearAllMocks(); });

function serve(failing) {
  api.crmGet.mockImplementation((path) => {
    for (const [prefix, status] of Object.entries(failing)) {
      if (path.startsWith(prefix)) return Promise.reject(Object.assign(new Error('x'), { status }));
    }
    return Promise.resolve({});
  });
}

describe('OurAITab live sources', () => {
  it('does not report an owner-only source as an outage to an agent (crawl finding, 403)', async () => {
    serve({ '/api/crm/routing/metrics': 403 });
    render(<OurAITab />);
    await waitFor(() => expect(api.crmGet).toHaveBeenCalledWith(expect.stringMatching(/routing\/metrics/), expect.anything()));
    // 13 sources, one owner-only: the agent's count excludes it rather than showing 12/13.
    expect(await screen.findByText(/12\/12 sources/)).toBeTruthy();
    expect(screen.queryByText('Partial live view')).toBeNull();
  });

  it('still reports a real outage', async () => {
    serve({ '/api/crm/routing/metrics': 503 });
    render(<OurAITab />);
    expect(await screen.findByText('Partial live view')).toBeTruthy();
    expect(screen.getByText(/Lead routing is temporarily unavailable/)).toBeTruthy();
  });
});

describe('OurAITab website status (T14.16)', () => {
  function serveSites(sites) {
    api.crmGet.mockImplementation((path) => Promise.resolve(path.startsWith('/api/sites') ? { sites } : {}));
    window.sessionStorage.setItem('oracle_ai_workspace', 'sites');
  }

  async function websiteStatus() {
    const name = await screen.findByText('Hyperlocal Website');
    const card = name.closest('li');
    await waitFor(() => expect(within(card).queryByText('Checking')).toBeNull());
    return card;
  }

  afterEach(() => window.sessionStorage.clear());

  it('is not Live just because the sites endpoint answered', async () => {
    serveSites([{ id: 's1', status: 'draft' }]);
    render(<OurAITab />);
    const card = await websiteStatus();
    expect(within(card).getByText('Setup required')).toBeTruthy();
    expect(within(card).queryByText('Live')).toBeNull();
  });

  it('is Live once a site is published', async () => {
    serveSites([{ id: 's1', status: 'draft' }, { id: 's2', status: 'published' }]);
    render(<OurAITab />);
    const card = await websiteStatus();
    expect(within(card).getByText('Live')).toBeTruthy();
  });
});
