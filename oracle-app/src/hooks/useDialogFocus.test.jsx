// @vitest-environment jsdom
import { useRef, useState } from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { useDialogFocus } from './useDialogFocus';

afterEach(cleanup);

function Harness() {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useDialogFocus(ref, open);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>Add client</button>
      <button type="button">Behind the scrim</button>
      {open && (
        <div ref={ref} role="dialog" aria-modal="true" aria-label="Add client">
          <input aria-label="Client name" />
          <button type="button" onClick={() => setOpen(false)}>Create Profile</button>
        </div>
      )}
    </>
  );
}

const frame = () => act(() => new Promise((resolve) => requestAnimationFrame(() => resolve())));

describe('useDialogFocus', () => {
  it('moves focus in, keeps Tab and Shift+Tab inside, and returns focus to the opener', async () => {
    render(<Harness />);
    const opener = screen.getByRole('button', { name: 'Add client' });
    opener.focus();
    fireEvent.click(opener);
    await frame();
    const name = screen.getByLabelText('Client name');
    const create = screen.getByRole('button', { name: 'Create Profile' });
    expect(document.activeElement).toBe(name);

    create.focus();
    fireEvent.keyDown(document.activeElement, { key: 'Tab' });
    expect(document.activeElement).toBe(name);
    fireEvent.keyDown(document.activeElement, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(create);

    // Focus that somehow lands behind the scrim is pulled back in.
    screen.getByRole('button', { name: 'Behind the scrim' }).focus();
    fireEvent.keyDown(document.activeElement, { key: 'Tab' });
    expect(document.activeElement).toBe(name);

    fireEvent.click(create);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(opener);
  });
});
