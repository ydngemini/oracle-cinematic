import { describe, expect, it } from 'vitest';

import {
  defaultSpeechLanguage, hintFor, nextSpeechLanguage, readSpeechLanguage, recognitionTag,
  saveSpeechLanguage, speechLanguageLabel,
} from './speechLanguage';

function memoryStore(initial = {}) {
  const data = { ...initial };
  return {
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => { data[k] = String(v); },
    data,
  };
}

const throwing = {
  getItem() { throw new Error('SecurityError'); },
  setItem() { throw new Error('QuotaExceededError'); },
};

describe('the default follows the browser', () => {
  it.each([
    ['es', 'es'], ['es-MX', 'es'], ['es-419', 'es'], ['ES-us', 'es'],
    ['en-US', 'en'], ['en-GB', 'en'], ['pt-BR', 'en'], ['', 'en'], [undefined, 'en'],
  ])('navigator.language %s → %s', (language, expected) => {
    expect(defaultSpeechLanguage({ language })).toBe(expected);
  });

  it('a browser with no navigator at all is English', () => {
    expect(defaultSpeechLanguage(undefined)).toBe('en');
  });

  it('"estonian-like" tags are not Spanish', () => {
    expect(defaultSpeechLanguage({ language: 'et-EE' })).toBe('en');
    expect(defaultSpeechLanguage({ language: 'eu' })).toBe('en');
  });
});

describe('the choice is remembered per device', () => {
  it('a saved choice beats the browser default', () => {
    const store = memoryStore();
    saveSpeechLanguage('es', store);
    expect(readSpeechLanguage({ language: 'en-US' }, store)).toBe('es');
    saveSpeechLanguage('en', store);
    expect(readSpeechLanguage({ language: 'es-MX' }, store)).toBe('en');
  });

  it('ignores anything that is not a launch language', () => {
    const store = memoryStore({ 'neoh.speechLanguage': 'fr' });
    expect(readSpeechLanguage({ language: 'es-ES' }, store)).toBe('es');
    saveSpeechLanguage('fr', store);
    expect(store.data['neoh.speechLanguage']).toBe('fr');
  });

  it('blocked storage never throws: it falls back to the default', () => {
    expect(readSpeechLanguage({ language: 'es-MX' }, throwing)).toBe('es');
    expect(() => saveSpeechLanguage('es', throwing)).not.toThrow();
  });
});

describe('recognition tags and the server hint', () => {
  it('English is en-US; Spanish keeps the browser\'s own region, else US Spanish', () => {
    expect(recognitionTag('en', { language: 'es-MX' })).toBe('en-US');
    expect(recognitionTag('es', { language: 'es-MX' })).toBe('es-MX');
    expect(recognitionTag('es', { language: 'es_ES' })).toBe('es-ES');
    expect(recognitionTag('es', { language: 'en-US' })).toBe('es-US');
  });

  it.each([
    ['es-MX', 'es'], ['es-US', 'es'], ['en-US', 'en'], ['en', 'en'], ['fr-FR', null], ['', null], [undefined, null],
  ])('hint for %s is %s', (tag, expected) => {
    expect(hintFor(tag)).toBe(expected);
  });

  it('the switch toggles and says what it will do', () => {
    expect(nextSpeechLanguage('en')).toBe('es');
    expect(nextSpeechLanguage('es')).toBe('en');
    expect(speechLanguageLabel('en')).toBe('Voice language: English. Switch to Spanish');
    expect(speechLanguageLabel('es')).toBe('Voice language: Spanish. Switch to English');
  });
});
