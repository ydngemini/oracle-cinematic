// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ErrorBoundary } from './ErrorBoundary';
import { AgentStatusBar } from './AgentStatusBar';

const assistant = vi.hoisted(() => ({ value: null }));
vi.mock('./AssistantContext', () => ({ useOptionalAssistant: () => assistant.value }));

afterEach(cleanup);
beforeEach(() => { vi.spyOn(console, 'error').mockImplementation(() => {}); });

function Boom() {
  throw new Error('TypeError: cannot read properties of undefined (reading "tenant_id")');
}

describe('ErrorBoundary', () => {
  it('a labelled section fails inline, in product language, with the raw message tucked away', () => {
    const { container } = render(
      <div>
        <p>Still here</p>
        <ErrorBoundary label="record sheet"><Boom /></ErrorBoundary>
      </div>,
    );
    expect(screen.getByText('Still here')).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'This section hit a problem' })).toBeTruthy();
    expect(screen.getByText(/rest of Neoh still works/)).toBeTruthy();
    // No HUD language.
    expect(container.textContent).not.toMatch(/Fault|subsystem|console/i);
    // The technical message is inside a collapsed disclosure, not the headline.
    const details = container.querySelector('details');
    expect(details).toBeTruthy();
    expect(details.open).toBe(false);
    expect(details.textContent).toMatch(/record sheet: TypeError/);
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Reload page' })).toBeTruthy();
  });

  it('Try again re-renders the children', () => {
    let fail = true;
    function Flaky() {
      if (fail) throw new Error('first render failed');
      return <p>Recovered</p>;
    }
    render(<ErrorBoundary label="card"><Flaky /></ErrorBoundary>);
    fail = false;
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(screen.getByText('Recovered')).toBeTruthy();
  });

  it('the app-level boundary (no label) takes the screen', () => {
    render(<ErrorBoundary><Boom /></ErrorBoundary>);
    expect(screen.getByRole('heading', { name: 'Something went wrong' })).toBeTruthy();
  });
});

describe('AgentStatusBar', () => {
  it('renders nothing while idle — no fake pipeline labels', () => {
    assistant.value = { commandStatus: { state: 'idle', message: 'Assistant ready' } };
    const { container } = render(<AgentStatusBar />);
    expect(container.textContent).toBe('');
  });

  it('describes a failure in words, not colour alone', () => {
    assistant.value = { commandStatus: { state: 'failed', message: '' } };
    render(<AgentStatusBar />);
    expect(screen.getByRole('status').textContent).toMatch(/couldn't finish that/);
  });

  it('shows the running request plainly, without a glyph scramble', () => {
    assistant.value = { commandStatus: { state: 'analyzing', message: 'Drafting the follow-up email' } };
    render(<AgentStatusBar />);
    expect(screen.getByRole('status').textContent).toBe('Drafting the follow-up email');
  });
});
