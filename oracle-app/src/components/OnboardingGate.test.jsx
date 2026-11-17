// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../state', () => ({
  ACTIONS: { PROFILE_SAVED: 'PROFILE_SAVED' },
  useOracleState: () => ({ memorySync: true, targetMarkets: [] }),
  useOracleDispatch: () => ({ dispatch: vi.fn() }),
}));
vi.mock('../lib/apiClient', () => ({ apiPost: vi.fn() }));
const { OnboardingGate } = await import('./OnboardingGate');

beforeEach(() => { sessionStorage.clear(); });
afterEach(() => { cleanup(); sessionStorage.clear(); });

describe('OnboardingGate', () => {
  it('the onboarding dialog keeps, moves and returns keyboard focus', () => {
    const outside = document.createElement('button');
    outside.textContent = 'outside';
    document.body.appendChild(outside);
    outside.focus();
    expect(document.activeElement).toBe(outside);

    render(<OnboardingGate />);
    const dialog = screen.getByRole('dialog');
    const focusables = [...dialog.querySelectorAll('button:not([disabled]), input:not([disabled])')];
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    expect(first).toBeTruthy();
    expect(last).not.toBe(first);

    // (1) focus moves into the dialog on open
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(document.activeElement).toBe(first);

    // (2) Tab from the last element wraps to the first; Shift+Tab from the first wraps to the last
    last.focus();
    fireEvent.keyDown(dialog, { key: 'Tab' });
    expect(document.activeElement).toBe(first);
    first.focus();
    fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(last);

    // (3) Escape dismisses exactly like "Later"
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(sessionStorage.getItem('oracle_onboarding_dismissed')).toBe('1');

    // (4) focus returns to the element focused before opening
    expect(document.activeElement).toBe(outside);

    outside.remove();
  });
});
