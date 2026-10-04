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

  it('gives a policy refusal its real reason instead of "try again"', () => {
    const refused = commandReceipt({
      ...sms('failed'),
      last_error: 'Text message blocked: connect and finish setting up text messages for your business number before sending.',
    });
    expect(refused.title).toBe('Text not sent');
    expect(refused.detail).toMatch(/Connect and finish setting up text messages/);
    expect(refused.detail).not.toMatch(/try again/);
    const quiet = commandReceipt({
      command_type: 'CALL', state: 'failed',
      last_error: 'Call blocked: outside the 8am-8pm calling window (recipient local time 06:10 EDT)',
    });
    expect(quiet.detail).toMatch(/Outside the 8am-8pm calling window/);
    // A provider's own error is not shown to the agent.
    expect(commandReceipt({ ...sms('failed'), last_error: 'provider returned HTTP 500: {...}' }).detail)
      .toMatch(/You can try again/);
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
