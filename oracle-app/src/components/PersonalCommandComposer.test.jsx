// @vitest-environment jsdom
/**
 * The command status this box sets is a source for Neoh's face (avatarFacts):
 * it must be stamped, must mark only a real execute confirmation as a
 * receipt, and must not leave "awaiting approval" behind when the box goes.
 */
import { useEffect } from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { crmPost } from '../state/useCrmApi';
import { AssistantProvider, useAssistant } from './AssistantContext';
import { PersonalCommandComposer } from './PersonalCommandComposer';

vi.mock('../state/useCrmApi', () => ({ crmPost: vi.fn() }));

let seen = { current: null };
function Status() {
  const { commandStatus } = useAssistant();
  useEffect(() => { seen = { current: commandStatus }; });
  return null;
}

function Harness({ show = true }) {
  return (
    <AssistantProvider>
      <Status />
      {show && <PersonalCommandComposer />}
    </AssistantProvider>
  );
}

async function prepare(text = 'text Sarah that the showing moved') {
  fireEvent.change(screen.getByRole('textbox'), { target: { value: text } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Prepare Draft/ })); });
}

beforeEach(() => {
  vi.clearAllMocks();
  window.requestAnimationFrame = (fn) => setTimeout(fn, 0);
});

afterEach(cleanup);

describe('PersonalCommandComposer command status', () => {
  it('stamps every status with when it was set, in order', async () => {
    crmPost.mockResolvedValueOnce({ requires_approval: true, intent: 'send_sms', draft_payload: {} });
    render(<Harness />);
    expect(seen.current).toMatchObject({ state: 'idle', at: 0 });
    await prepare();
    expect(seen.current.state).toBe('awaiting_approval');
    expect(seen.current.at).toBeGreaterThan(0);
    expect(seen.current.seq).toBeGreaterThan(1);
  });

  it('marks "analysis complete" as not a receipt', async () => {
    crmPost.mockResolvedValueOnce({ requires_approval: false, intent: 'lookup', draft_payload: {} });
    render(<Harness />);
    await prepare();
    expect(seen.current.state).toBe('completed');
    expect(seen.current.receipt).toBeUndefined();
  });

  it('marks only a confirmed execute as a receipt; a queued one is not', async () => {
    crmPost
      .mockResolvedValueOnce({ requires_approval: true, intent: 'save_document', draft_payload: {} })
      .mockResolvedValueOnce({ status: 'ENCRYPTED_IN_VAULT', command: { command_type: 'save_document' } })
      .mockResolvedValueOnce({ requires_approval: true, intent: 'send_sms', draft_payload: {} })
      .mockResolvedValueOnce({ status: 'QUEUED', command: { command_type: 'send_sms' } });
    render(<Harness />);
    await prepare();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Authorize/ })); });
    expect(seen.current).toMatchObject({ state: 'completed', receipt: true });
    await prepare();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Authorize/ })); });
    expect(seen.current).toMatchObject({ state: 'queued', receipt: false });
  });

  it('clears an abandoned approval when the box unmounts', async () => {
    crmPost.mockResolvedValueOnce({ requires_approval: true, intent: 'send_sms', draft_payload: {} });
    const { rerender } = render(<Harness />);
    await prepare();
    expect(seen.current.state).toBe('awaiting_approval');
    rerender(<Harness show={false} />);
    expect(seen.current.state).toBe('idle');
  });
});
