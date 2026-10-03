import { describe, expect, it } from 'vitest';

import {
  commandMessageId, commandReceipt, handledPhrase, receiptTitle, receiptsByMessage,
} from './actionLabels';

const MESSAGE = 'aaaaaaaa-1111-4111-8111-111111111111';

describe('receiptTitle / handledPhrase', () => {
  it('turns tool names into product verbs', () => {
    expect(receiptTitle({ action_type: 'add_client_note' })).toBe('Note added');
    expect(receiptTitle({ action_type: 'update_listing' })).toBe('Listing updated');
    expect(handledPhrase('update_listing', 1)).toBe('listing updated');
    expect(handledPhrase('add_client_note', 3)).toBe('notes added');
  });

  it('falls back to the record it touched, then to a generic line — never a raw tool name in a receipt', () => {
    expect(receiptTitle({ record_type: 'client' })).toBe('Client updated');
    expect(receiptTitle({ record_type: 'lead' })).toBe('Deal updated');
    expect(receiptTitle({})).toBe('Record updated');
    expect(handledPhrase('some_new_tool', 2)).toBe('some new tool');
  });
});

describe('commandReceipt', () => {
  const sms = (state) => ({ command_type: 'SMS', state, target: { phone: '+15555550100' } });

  it('says what actually happened, per state', () => {
    expect(commandReceipt(sms('awaiting_approval'))).toMatchObject({ title: 'Text waiting for your approval', review: true });
    expect(commandReceipt(sms('awaiting_approval')).detail).toMatch(/Nothing has been sent/);
    expect(commandReceipt(sms('executing')).title).toBe('Sending text…');
    expect(commandReceipt(sms('succeeded')).title).toBe('Text sent');
    expect(commandReceipt({ command_type: 'CALL', state: 'succeeded' }).title).toBe('Call placed');
    expect(commandReceipt({ command_type: 'CALENDAR', state: 'succeeded' }).title).toBe('Added to calendar');
    expect(commandReceipt(sms('failed')).title).toBe("Text didn't go through");
    expect(commandReceipt(sms('cancelled')).detail).toMatch(/Nothing was sent/);
  });

  it('never reads an uncertain send as success or failure', () => {
    const receipt = commandReceipt(sms('reconciliation_required'));
    expect(receipt.title).toBe('Text needs review');
    expect(receipt.tone).toBe('review');
    expect(receipt.review).toBe(true);
    expect(receipt.detail).toMatch(/couldn't confirm/);
  });

  it('has no receipt for a state it does not know', () => {
    expect(commandReceipt({ command_type: 'SMS', state: 'mystery' })).toBeNull();
  });
});

describe('receiptsByMessage', () => {
  it('joins commands to the chat turn that staged them by idempotency key', () => {
    const commands = [
      { id: '2', created_at: '2026-10-03T10:02:00Z', idempotency_key: `ai:${MESSAGE}:draft_email:x` },
      { id: '1', created_at: '2026-10-03T10:01:00Z', idempotency_key: `ai:${MESSAGE.toUpperCase()}:draft_sms:x` },
      { id: '3', idempotency_key: 'assistant:not-from-chat' },
      { id: '4' },
    ];
    expect(commandMessageId(commands[0])).toBe(MESSAGE);
    expect(commandMessageId(commands[2])).toBeNull();
    const map = receiptsByMessage(commands);
    expect([...map.keys()]).toEqual([MESSAGE]);
    expect(map.get(MESSAGE).map((c) => c.id)).toEqual(['1', '2']);
  });
});
