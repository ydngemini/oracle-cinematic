// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { UniversalWorkspace } from './UniversalWorkspace';
import { crmGet } from '../state/useCrmApi';

vi.mock('../state/useCrmApi', () => ({ crmGet: vi.fn() }));
vi.mock('../components/PeopleTab', () => ({ default: () => <div data-testid="people-tab">People Tab</div> }));
vi.mock('../components/DealsTab', () => ({ default: () => <div data-testid="deals-tab">Deals Tab</div> }));
vi.mock('../components/PropertiesTab', () => ({ default: () => <div data-testid="properties-tab">Properties Tab</div> }));
vi.mock('../components/CommsTab', () => ({ default: () => <div data-testid="comms-tab">Comms Tab</div> }));

const sampleRecent = [
  { kind: 'people', id: 'p1', label: 'Sarah Chen', sublabel: 'Buyer · spoke yesterday', href: '/p/p1' },
  { kind: 'deals', id: 'd1', label: '1832 Cedar Run', sublabel: '8 buyer matches', href: '/deal/d1' },
];

beforeEach(() => {
  vi.stubGlobal('requestAnimationFrame', (callback) => setTimeout(callback, 0));
  vi.stubGlobal('cancelAnimationFrame', clearTimeout);
  crmGet.mockImplementation((path) => {
    if (path.includes('/api/search/recent')) {
      return Promise.resolve({ results: sampleRecent, degraded: false });
    }
    return Promise.resolve({ results: [], counts: {}, degraded: [] });
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('UniversalWorkspace', () => {
  it('renders Recent items when type is recent', async () => {
    const onOpenEntity = vi.fn();
    render(<UniversalWorkspace type="recent" onOpenEntity={onOpenEntity} />);

    await waitFor(() => {
      expect(screen.getByText('Sarah Chen')).toBeTruthy();
      expect(screen.getByText('1832 Cedar Run')).toBeTruthy();
    });

    fireEvent.click(screen.getByText('Sarah Chen'));
    expect(onOpenEntity).toHaveBeenCalledWith('/p/p1');
  });

  it('shows Recent chip as selected at rest and toggles kind', async () => {
    const onNavigate = vi.fn();
    render(<UniversalWorkspace type="recent" onNavigate={onNavigate} />);

    const recentChip = screen.getByRole('tab', { name: 'Recent' });
    expect(recentChip.getAttribute('aria-selected')).toBe('true');

    const peopleChip = screen.getByRole('tab', { name: 'People' });
    expect(peopleChip.getAttribute('aria-selected')).toBe('false');

    fireEvent.click(peopleChip);
    expect(onNavigate).toHaveBeenCalledWith('people', { q: '' });
  });

  it('clicking an active kind chip toggles back to recent', () => {
    const onNavigate = vi.fn();
    render(<UniversalWorkspace type="people" onNavigate={onNavigate} />);

    const peopleChip = screen.getByRole('tab', { name: 'People' });
    expect(peopleChip.getAttribute('aria-selected')).toBe('true');

    fireEvent.click(peopleChip);
    expect(onNavigate).toHaveBeenCalledWith('recent', { q: '' });
  });

  it('hides Recent chip when actively searching', async () => {
    render(<UniversalWorkspace type="recent" query="1832" />);

    expect(screen.queryByRole('tab', { name: 'Recent' })).toBeNull();
    expect(screen.getByRole('tab', { name: 'People' })).toBeTruthy();
    expect(screen.getByRole('tab', { name: 'Properties' })).toBeTruthy();
  });
});
