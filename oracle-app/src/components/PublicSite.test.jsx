// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PublicSite from './PublicSite';

const SITE = {
  name: 'Wilmington Homes', template_key: 'editorial', revision: 3,
  brand_theme: {}, content: {
    headline: 'Find your Wilmington home', description: 'Local guidance.',
    requested_service_areas: ['Trolley Square'],  // requested, not source-backed
  },
};

beforeEach(() => {
  window.history.pushState({}, '', '/sites/northstar/wilmington-homes');
  window.matchMedia = window.matchMedia || ((query) => ({
    matches: false, media: query, addEventListener() {}, removeEventListener() {},
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('PublicSite', () => {
  it('shows the published page and nothing that is not ready for the public', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => SITE });
    vi.stubGlobal('fetch', fetchMock);
    render(<PublicSite />);
    expect(await screen.findByText('Find your Wilmington home')).toBeTruthy();
    expect(fetchMock.mock.calls[0][0]).toBe('/api/public/sites/northstar/wilmington-homes');
    const text = document.body.textContent;
    expect(text).not.toMatch(/Private preview|pending|IDX|Trolley Square/i);
    expect(text).toMatch(/Published with Neoh/);
  });

  it('says a page is not published instead of showing a broken site', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404 }));
    render(<PublicSite />);
    expect(await screen.findByText('This page is not published.')).toBeTruthy();
  });
});
