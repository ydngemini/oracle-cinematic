// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmGet: vi.fn() }));
vi.mock('../state/useCrmApi', () => api);
const { ServiceStatusBanner } = await import('./ServiceStatusBanner');

afterEach(cleanup);

describe('ServiceStatusBanner', () => {
  it('shows the product-language messages the server sends', async () => {
    api.crmGet.mockResolvedValue({ state: 'DEGRADED', messages: ['Listing data may be out of date.'] });
    render(<ServiceStatusBanner />);
    expect(await screen.findByText('Listing data may be out of date.')).toBeTruthy();
  });

  it('marks itself so the shell can place it below the fixed header', async () => {
    api.crmGet.mockResolvedValue({ state: 'DEGRADED', messages: ['Some emails are waiting to send.'] });
    render(<ServiceStatusBanner />);
    const banner = await screen.findByRole('status');
    expect(banner.hasAttribute('data-service-banner')).toBe(true);
  });

  it('renders nothing when everything is healthy', async () => {
    api.crmGet.mockResolvedValue({ state: 'HEALTHY', messages: [] });
    const { container } = render(<ServiceStatusBanner />);
    await new Promise((r) => setTimeout(r, 0));
    expect(container.textContent).toBe('');
  });

  it('stays out of the way if the status call itself fails', async () => {
    api.crmGet.mockRejectedValue(new Error('503'));
    const { container } = render(<ServiceStatusBanner />);
    await new Promise((r) => setTimeout(r, 0));
    expect(container.textContent).toBe('');
  });
});
