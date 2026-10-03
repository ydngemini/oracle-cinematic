// @vitest-environment jsdom
/**
 * What each of the three destinations renders.
 *
 * The Neoh tab rendered the old AI hub (OurAITab, opened on "Cowork") with a
 * note that the conversation would land later. It is the conversation now,
 * the hub is a Work view, old /our-ai addresses still resolve, and the
 * floating composer stands down on the tab that already is one.
 */

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { CrmShell } from './CrmShell';

const { pass } = vi.hoisted(() => ({ pass: ({ children }) => children }));
vi.mock('./BillingOverlay', () => ({ BillingOverlay: () => null }));
vi.mock('./ServiceStatusBanner', () => ({ ServiceStatusBanner: () => null }));
vi.mock('./OnboardingGate', () => ({ OnboardingGate: () => null }));
vi.mock('./StateSelector', () => ({ StateSelector: () => null }));
vi.mock('./NeohBrandMark', () => ({ NeohBrandMark: () => null }));
vi.mock('./NeohFooter', () => ({ NeohFooter: () => null }));
vi.mock('./ProductTour', () => ({ ProductTour: () => null }));
vi.mock('./motion/BorderBeam', () => ({ BorderBeam: () => null }));
vi.mock('./motion/AdaptiveViewTransition', () => ({ AdaptiveViewTransition: pass, hasHighMotionBudget: () => false }));
vi.mock('../state/StateContext', () => ({ StateProvider: pass }));
vi.mock('./AssistantContext', () => ({ AssistantProvider: pass }));
vi.mock('../neoh/NeohHome', () => ({ NeohHome: () => <div data-testid="home" /> }));
vi.mock('../neoh/NeohCorner', () => ({ NeohCorner: () => null }));
vi.mock('../neoh/NeohSurface', () => ({ NeohSurface: () => <div data-testid="floating-composer" /> }));
vi.mock('../neoh/NeohConversation', () => ({ NeohConversation: () => <div data-testid="conversation" /> }));
vi.mock('../neoh/UniversalWorkspace', () => ({
  UniversalWorkspace: ({ type }) => <div data-testid="work" data-type={type} />,
}));
vi.mock('./OurAITab', () => {
  globalThis.__ourAiImported = true;
  return { default: () => <div data-testid="our-ai" /> };
});

beforeEach(() => {
  window.sessionStorage.clear();
  window.localStorage.setItem('oracle_product_tour_v1', 'dismissed');
  window.matchMedia = (query) => ({
    matches: false, media: query, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  });
});

afterEach(() => {
  cleanup();
  window.history.replaceState({}, '', '/');
});

describe('CrmShell destinations', () => {
  it('the Neoh tab is the conversation, with no second floating composer', async () => {
    window.history.replaceState({}, '', '/neoh');
    render(<CrmShell />);
    expect(await screen.findByTestId('conversation')).toBeTruthy();
    expect(screen.queryByTestId('our-ai')).toBeNull();
    expect(screen.queryByTestId('floating-composer')).toBeNull();
    expect(globalThis.__ourAiImported).toBeUndefined();
    expect(screen.getByRole('tab', { name: 'Neoh' }).getAttribute('aria-selected')).toBe('true');
  });

  it('Home has the briefing and the floating composer', async () => {
    render(<CrmShell />);
    expect(await screen.findByTestId('home')).toBeTruthy();
    expect(await screen.findByTestId('floating-composer')).toBeTruthy();
  });

  it('an old /our-ai bookmark lands on the hub in Work, not on the Neoh tab', async () => {
    window.history.replaceState({}, '', '/our-ai');
    render(<CrmShell />);
    const work = await screen.findByTestId('work');
    expect(work.getAttribute('data-type')).toBe('ai');
    expect(window.location.pathname + window.location.search).toBe('/work?type=ai');
    expect(screen.queryByTestId('conversation')).toBeNull();
  });

  it('an old /our-ai/cowork bookmark lands on the conversation', async () => {
    window.history.replaceState({}, '', '/our-ai/cowork');
    render(<CrmShell />);
    expect(await screen.findByTestId('conversation')).toBeTruthy();
  });
});
