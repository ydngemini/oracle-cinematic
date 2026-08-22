// @vitest-environment jsdom
/**
 * The Undo button, and the one thing it must never be.
 *
 * `_is_record_change` broadcasts every successful non-read tool to this
 * component as an applied receipt, but only the shared field-update path in
 * ai_chat_store wrote an `ai_chat_actions` row. So six tools — add_client_note,
 * add_client_tag, move_deal_stage, archive_client, create_client and
 * create_deal_note — mutated a record, arrived here with no `action_id`, and
 * rendered a button that POSTed to /api/ai/chat/actions/undefined/undo.
 *
 * P11 gives every mutation a ledger row and a declared undo_kind. Two of those
 * kinds are reversible and one is not, so the button now has to read what the
 * receipt actually says rather than assume.
 */

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AssistantMessages } from './AssistantMessages';

afterEach(cleanup);

function receipt(overrides = {}) {
  return {
    action_id: 'aaaaaaaa-0000-0000-0000-000000000000',
    action_type: 'add_client_note',
    record_type: 'client',
    record_id: 'c1',
    undoable: true,
    undo_expires_at: '2026-08-19T00:00:00Z',
    detail: 'Note recorded in the client activity feed.',
    ...overrides,
  };
}

function renderWith(actions) {
  return render(
    <AssistantMessages
      messages={[{ id: 'm1', role: 'assistant', content: 'Done.', actions }]}
      onUndo={vi.fn()}
      undoing={null}
    />,
  );
}

describe('ActionReceipt', () => {
  it('offers Undo for a reversible action', () => {
    renderWith([receipt()]);
    expect(screen.getByRole('button', { name: 'Undo' })).toBeTruthy();
  });

  it('offers no Undo when the receipt says the action is not reversible', () => {
    // create_client: deleting a client cascades to ten tables, so it is
    // recorded for audit and declares itself irreversible.
    renderWith([receipt({
      action_type: 'create_client',
      undoable: false,
      undo_expires_at: null,
      undo_unavailable_reason:
        'Deleting a client would cascade to everything attached to it since. '
        + 'Use archive_client instead, which is reversible.',
    })]);

    expect(screen.queryByRole('button', { name: 'Undo' })).toBeNull();
    expect(screen.getByText(/cascade to everything attached/)).toBeTruthy();
  });

  it('offers no Undo when the receipt carries no action id', () => {
    // The exact defect: a mutation that reached the UI without a ledger row.
    // The button would have posted to .../actions/undefined/undo.
    renderWith([receipt({ action_id: undefined })]);
    expect(screen.queryByRole('button', { name: 'Undo' })).toBeNull();
  });

  it('states why an irreversible action cannot be undone rather than staying silent', () => {
    renderWith([receipt({ undoable: false, undo_unavailable_reason: undefined })]);
    expect(screen.queryByRole('button', { name: 'Undo' })).toBeNull();
    expect(screen.getByText(/cannot be undone from here/)).toBeTruthy();
  });

  it('shows what changed for a field update and what happened for everything else', () => {
    renderWith([
      receipt({ action_type: 'set_client_stage', fields: { stage: 'active' }, detail: undefined }),
      receipt({ action_id: 'bbbbbbbb-0000-0000-0000-000000000000' }),
    ]);

    expect(screen.getByText(/stage: active/)).toBeTruthy();
    expect(screen.getByText('Note recorded in the client activity feed.')).toBeTruthy();
  });

  it('drops the button once the action is undone', () => {
    renderWith([receipt({ status: 'undone' })]);
    expect(screen.queryByRole('button', { name: 'Undo' })).toBeNull();
    expect(screen.getByText('Change undone')).toBeTruthy();
  });
});
