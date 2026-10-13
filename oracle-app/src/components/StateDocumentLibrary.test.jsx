// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmGet: vi.fn() }));
vi.mock('../state/useCrmApi', () => api);

const { StateRegulatoryFacts } = await import('./StateDocumentLibrary');

afterEach(() => { cleanup(); vi.clearAllMocks(); });

function serve(ads) {
  api.crmGet.mockImplementation((path) => {
    if (path.includes('/advertising-rules')) return ads();
    return Promise.resolve(null);
  });
}

describe('State advertising rules (crawl finding)', () => {
  it('never says "none recorded" when the rules were simply not loaded', async () => {
    serve(() => Promise.reject(Object.assign(new Error('not loaded'), { status: 404 })));
    render(<StateRegulatoryFacts stateCode="DE" />);
    expect(await screen.findByText(/haven.t been loaded yet/)).toBeTruthy();
    expect(screen.queryByText('No advertising rules recorded for this state.')).toBeNull();
  });

  it('says a load failure is a load failure', async () => {
    serve(() => Promise.reject(Object.assign(new Error('down'), { status: 503 })));
    render(<StateRegulatoryFacts stateCode="DE" />);
    expect(await screen.findByText(/couldn.t be loaded right now/)).toBeTruthy();
  });

  it('keeps "none recorded" for a real empty answer', async () => {
    serve(() => Promise.resolve([]));
    render(<StateRegulatoryFacts stateCode="DE" />);
    expect(await screen.findByText('No advertising rules recorded for this state.')).toBeTruthy();
  });
});
