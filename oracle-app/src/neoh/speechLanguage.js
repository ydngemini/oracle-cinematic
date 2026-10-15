/**
 * Which language the microphone listens in (L05-05).
 *
 * Browser speech recognition has to be told the language BEFORE it listens:
 * a recogniser set to en-US hears "¿qué está frenando 123 Main?" as English
 * noise. Launch languages are English and Spanish (owner, 2026-10-14).
 *
 * - Default: the browser's own language when it is Spanish
 *   (`navigator.language` starts with "es"), otherwise English.
 * - The person can switch next to the mic; the choice is remembered on this
 *   device only (localStorage, wrapped: a private window or blocked storage
 *   just falls back to the default). There is deliberately no server-side
 *   user setting.
 * - A spoken turn tells the server the language it was recognised in, as a
 *   HINT ('en' | 'es'). The server still decides from the words whenever
 *   they are conclusive.
 */

export const SPEECH_LANGUAGES = Object.freeze(['en', 'es']);

const STORAGE_KEY = 'neoh.speechLanguage';

/** Spoken names, used in the switch's accessible label. */
const NAMES = { en: 'English', es: 'Spanish' };

function browserLanguage(nav) {
  try {
    const value = nav?.language;
    return typeof value === 'string' ? value : '';
  } catch {
    return '';
  }
}

function isSpanishTag(tag) {
  return /^es(?:[-_]|$)/i.test(tag || '');
}

/** 'es' when the browser itself is Spanish, else 'en'. */
export function defaultSpeechLanguage(nav = globalThis.navigator) {
  return isSpanishTag(browserLanguage(nav)) ? 'es' : 'en';
}

/** The remembered choice on this device, or the default. Never throws. */
export function readSpeechLanguage(nav = globalThis.navigator, storage) {
  try {
    const store = storage ?? globalThis.localStorage;
    const saved = store?.getItem(STORAGE_KEY);
    if (SPEECH_LANGUAGES.includes(saved)) return saved;
  } catch {
    /* storage blocked or unavailable: use the default */
  }
  return defaultSpeechLanguage(nav);
}

/** Remember the choice on this device. Never throws. */
export function saveSpeechLanguage(lang, storage) {
  if (!SPEECH_LANGUAGES.includes(lang)) return;
  try {
    (storage ?? globalThis.localStorage)?.setItem(STORAGE_KEY, lang);
  } catch {
    /* storage blocked: the choice lasts for this page only */
  }
}

/**
 * The BCP 47 tag to give SpeechRecognition.lang. Spanish keeps the browser's
 * own regional tag when it has one (es-MX hears Mexican Spanish best);
 * otherwise US Spanish, the market this product serves.
 */
export function recognitionTag(lang, nav = globalThis.navigator) {
  if (lang !== 'es') return 'en-US';
  const own = browserLanguage(nav);
  return isSpanishTag(own) ? own.replace('_', '-') : 'es-US';
}

/** 'en' | 'es' for a recognition tag, or null for anything else. */
export function hintFor(tag) {
  if (isSpanishTag(tag)) return 'es';
  if (/^en(?:[-_]|$)/i.test(tag || '')) return 'en';
  return null;
}

export function nextSpeechLanguage(lang) {
  return lang === 'es' ? 'en' : 'es';
}

/** The switch's label: what it is now, and what pressing it does. */
export function speechLanguageLabel(lang) {
  const now = NAMES[lang] || NAMES.en;
  const next = NAMES[nextSpeechLanguage(lang)];
  return `Voice language: ${now}. Switch to ${next}`;
}
