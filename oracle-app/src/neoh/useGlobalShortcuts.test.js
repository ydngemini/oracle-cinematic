import { describe, expect, it } from 'vitest';

import { isEditable, shortcutFor } from './useGlobalShortcuts';

const key = (props) => ({ metaKey: false, ctrlKey: false, altKey: false, target: null, ...props });

describe('shortcutFor', () => {
  it('⌘K and Ctrl+K focus Neoh from anywhere, even inside a field', () => {
    expect(shortcutFor(key({ key: 'k', metaKey: true }))).toBe('focus');
    expect(shortcutFor(key({ key: 'K', ctrlKey: true, target: { tagName: 'INPUT' } }))).toBe('focus');
  });

  it('a bare slash focuses only when the person is not already typing', () => {
    expect(shortcutFor(key({ key: '/' }))).toBe('focus');
    expect(shortcutFor(key({ key: '/', target: { tagName: 'TEXTAREA' } }))).toBeNull();
    expect(shortcutFor(key({ key: '/', target: { tagName: 'DIV', isContentEditable: true } }))).toBeNull();
    expect(shortcutFor(key({ key: '/', altKey: true }))).toBeNull();
  });

  it('Escape is escape; everything else is nothing', () => {
    expect(shortcutFor(key({ key: 'Escape' }))).toBe('escape');
    expect(shortcutFor(key({ key: 'a' }))).toBeNull();
    expect(shortcutFor(null)).toBeNull();
  });

  it('knows what counts as editable', () => {
    expect(isEditable({ tagName: 'select' })).toBe(true);
    expect(isEditable({ tagName: 'BUTTON' })).toBe(false);
    expect(isEditable(null)).toBe(false);
  });
});
