// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { ScoreMeter } from './ClientShared';

afterEach(cleanup);

describe('ScoreMeter', () => {
  it('is announced as one labelled image, not a role-less span with a prohibited aria-label', () => {
    render(<ScoreMeter score={72} />);
    const meter = screen.getByRole('img', { name: 'Lead score 72 of 100' });
    expect(meter.getAttribute('role')).toBe('img');
  });

  it('renders nothing without a score', () => {
    const { container } = render(<ScoreMeter score={null} />);
    expect(container.textContent).toBe('');
  });
});
