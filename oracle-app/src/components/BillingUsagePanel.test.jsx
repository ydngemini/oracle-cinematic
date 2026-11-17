// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmGet: vi.fn() }));
vi.mock('../state/useCrmApi', () => api);

const { default: BillingUsagePanel } = await import('./BillingUsagePanel');

afterEach(() => { cleanup(); vi.clearAllMocks(); });

function metricsFor(name) {
  return {
    metered_billing_enabled: false,
    metrics: [{
      metric: name, events: 2, quantity: 5, reported_to_stripe: 0, last_at: null,
    }],
  };
}

/** One deferred promise per `days` value, so the test decides the arrival order. */
function deferPerWindow() {
  const pending = {};
  api.crmGet.mockImplementation((url) => {
    const days = new URL(url, 'http://x').searchParams.get('days');
    return new Promise((resolve, reject) => { pending[days] = { resolve, reject }; });
  });
  return pending;
}

async function openNinetyWhileThirtyIsInFlight(pending) {
  render(<BillingUsagePanel />);
  await waitFor(() => expect(pending['30']).toBeDefined());
  fireEvent.click(screen.getByRole('button', { name: '90d' }));
  await waitFor(() => expect(pending['90']).toBeDefined());
  expect(screen.getByRole('button', { name: '90d' }).getAttribute('aria-pressed')).toBe('true');
}

describe('BillingUsagePanel', () => {
  it('a superseded usage response never overwrites the current window', async () => {
    const pending = deferPerWindow();
    await openNinetyWhileThirtyIsInFlight(pending);

    await act(async () => { pending['90'].resolve(metricsFor('ninety')); });
    await waitFor(() => expect(screen.getByText('ninety')).toBeTruthy());

    await act(async () => { pending['30'].resolve(metricsFor('thirty')); });

    expect(screen.getByRole('button', { name: '90d' }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.queryByText('thirty')).toBeNull();
    expect(screen.getByText('ninety')).toBeTruthy();
  });

  it('a superseded request error does not replace current data', async () => {
    const pending = deferPerWindow();
    await openNinetyWhileThirtyIsInFlight(pending);

    await act(async () => { pending['90'].resolve(metricsFor('ninety')); });
    await waitFor(() => expect(screen.getByText('ninety')).toBeTruthy());

    await act(async () => { pending['30'].reject(new Error('stale failure')); });

    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByText('ninety')).toBeTruthy();
  });
});
