// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import EmailConnectPanel from './EmailConnectPanel';

afterEach(cleanup);

const props = (over = {}) => ({
  provider: { provider: 'smtp', configured: false }, platformSender: 'no-reply@neohrs.com', working: '',
  onConnect: vi.fn().mockResolvedValue(true), onSendTest: vi.fn(), onDisconnect: vi.fn(), ...over,
});

describe('EmailConnectPanel', () => {
  it('connects a Gmail address with an app password in one step', async () => {
    const p = props();
    render(<EmailConnectPanel {...p} />);
    expect(screen.getByText(/go out from no-reply@neohrs.com, with replies sent to you/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Email address'), { target: { value: 'avery@gmail.com' } });
    fireEvent.change(screen.getByLabelText('App password'), { target: { value: 'abcd efgh ijkl mnop' } });
    fireEvent.click(screen.getByRole('button', { name: /connect/i }));
    await waitFor(() => expect(p.onConnect).toHaveBeenCalledTimes(1));
    expect(p.onConnect.mock.calls[0][0]).toMatchObject({ host: 'smtp.gmail.com', from_email: 'avery@gmail.com', password: 'abcdefghijklmnop' });
  });

  it('switches to the right steps from the address, and refuses personal Outlook up front', () => {
    render(<EmailConnectPanel {...props()} />);
    fireEvent.change(screen.getByLabelText('Email address'), { target: { value: 'avery@hotmail.com' } });
    fireEvent.blur(screen.getByLabelText('Email address'));
    expect(screen.getByRole('note').textContent).toMatch(/can.t connect one yet/);
    expect(screen.queryByRole('button', { name: /^connect$/i })).toBeNull();
  });

  it("shows the agent's own address once connected, with a test send", () => {
    const p = props({ provider: { provider: 'smtp', configured: true, own: true, sending_as: 'avery@gmail.com', account_label: 'avery' } });
    render(<EmailConnectPanel {...p} />);
    expect(screen.getByText('Sending as avery@gmail.com')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /send me a test email/i }));
    expect(p.onSendTest).toHaveBeenCalled();
    expect(screen.queryByLabelText('Email address')).toBeNull();
  });

  it("says when mail goes out from the brokerage's address, not the agent's", () => {
    render(<EmailConnectPanel {...props({ provider: { provider: 'smtp', configured: true, own: false, sending_as: 'team@brokerage.com' } })} />);
    expect(screen.getByText(/team@brokerage.com, your brokerage.s email/)).toBeTruthy();
    expect(screen.getByLabelText('Email address')).toBeTruthy();
  });

  it('explains a refused login in words that say what to do', () => {
    render(<EmailConnectPanel {...props({ provider: { provider: 'smtp', configured: false, own: true, validation_status: 'invalid', validation_error: '(535, Username and Password not accepted)' } })} />);
    expect(screen.getByRole('alert').textContent).toMatch(/app password/);
  });
});
