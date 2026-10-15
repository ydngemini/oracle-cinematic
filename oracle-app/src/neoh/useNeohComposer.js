import { useCallback, useEffect, useRef, useState } from 'react';

import { crmPost } from '../state/useCrmApi';
import { isBusy } from './surfaceModel';
import { wireContext } from './useNeohChannel';
import { useSpeechInput } from './useSpeechInput';
import {
  hintFor, readSpeechLanguage, recognitionTag, saveSpeechLanguage,
} from './speechLanguage';

/**
 * useNeohComposer — one way to ask Neoh, shared by every place you can.
 *
 * The floating bar and the Neoh tab used to be able to drift: each would have
 * carried its own copy of "ask the deterministic path, fall through to the
 * model, put the text back if the channel refused it". Now there is one, and
 * typing and talking go through it identically — a finished utterance is the
 * same kind of turn as a typed one.
 *
 * One deliberate difference from before: speaking while Neoh is still
 * answering no longer fires a second turn on top of the first. The words go
 * into the field, where they can be read and sent — the microphone stays
 * usable while Neoh thinks, it just does not talk over it.
 *
 * Voice language (L05-05): the microphone listens in the language chosen
 * next to it (`speechLanguage.js`; English, or Spanish when the browser is),
 * remembered on this device. A SPOKEN turn tells the server which language
 * it was recognised in (`lang`), as a hint for the answer language; a typed
 * turn sends nothing new, so typing behaves exactly as before.
 */

export const MAX_DRAFT = 8_000;

export function useNeohComposer({ channel, record, onSettled, draftState }) {
  // The draft can be owned by a parent that outlives this composer (the
  // floating bar's panel unmounts when it closes; its draft must not).
  const ownDraft = useState('');
  const [draft, setDraft] = draftState || ownDraft;
  const [rendered, setRendered] = useState(null);
  const [asking, setAsking] = useState(false);
  const busy = asking || isBusy(channel.messages);
  const submitRef = useRef(null);
  const busyRef = useRef(busy);
  useEffect(() => { busyRef.current = busy; });

  const [speechLanguage, setSpeechLanguageState] = useState(() => readSpeechLanguage());
  const setSpeechLanguage = useCallback((lang) => {
    setSpeechLanguageState(lang);
    saveSpeechLanguage(lang);
  }, []);

  const speech = useSpeechInput({
    lang: recognitionTag(speechLanguage),
    onFinal: (text, tag) => {
      if (busyRef.current) {
        setDraft((current) => `${current.trim() ? `${current.trim()} ` : ''}${text}`.slice(0, MAX_DRAFT));
        return;
      }
      void submitRef.current?.(text, { lang: hintFor(tag) });
    },
  });

  const submit = async (override, { lang = null } = {}) => {
    const spoken = typeof override === 'string';
    const text = (spoken ? override : draft).trim();
    if (!text) return null;
    // A spoken turn must not eat a half-typed message: the field invites the
    // person to keep typing while the microphone is open.
    if (!spoken) setDraft('');
    setAsking(true);
    // The ask path is an optimisation, never a gate: if it fails, the
    // question still reaches the model.
    // The open record rides along: "who should I call about this?" asked on
    // a property is about that property, and only the model has it.
    const context = wireContext(record);
    // Only a spoken turn carries the language it was recognised in.
    const hint = spoken && lang ? { lang } : {};
    const answer = await crmPost('/api/neoh/ask', context ? { text, context, ...hint } : { text, ...hint })
      .catch(() => null);
    setAsking(false);
    if (answer && !answer.fallthrough && (answer.blocks || []).length > 0) {
      setRendered({ ...answer, question: text });
      onSettled?.('rendered');
      return 'rendered';
    }
    setRendered(null);
    if (!channel.send(text, record, [], hint)) {
      // Refused (reconnecting): put the text back rather than silently eating
      // it — but never over a draft the person is still typing.
      setDraft((current) => (current.trim() ? current : text));
      onSettled?.('refused');
      return 'refused';
    }
    onSettled?.('sent');
    return 'sent';
  };

  // Kept current so the speech hook reaches the latest submit without
  // depending on it and tearing down a live recognition session.
  useEffect(() => { submitRef.current = submit; });

  const onKeyDown = useCallback((event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent?.isComposing) {
      event.preventDefault();
      void submitRef.current?.();
    }
  }, []);

  const toggleMic = useCallback(() => {
    if (speech.state === 'error') speech.clearError();
    if (speech.state === 'listening' || speech.state === 'requesting') speech.stop();
    else speech.start();
  }, [speech]);

  const stage = useCallback((value) => setDraft(String(value ?? '').slice(0, MAX_DRAFT)), [setDraft]);

  return {
    draft,
    setDraft: stage,
    rendered,
    setRendered,
    asking,
    busy,
    speech,
    speechLanguage,
    setSpeechLanguage,
    submit,
    onKeyDown,
    toggleMic,
  };
}

/**
 * The single polite announcement for a finished reply.
 *
 * The message list used to be the live region, with `aria-relevant="additions
 * text"`, so every streamed chunk was re-read — and the interim voice caption
 * was a second live region re-reading every partial word. Now nothing that
 * streams is live. This watches for a turn that this surface saw in flight
 * and announces it once, when it completes or fails. History that hydrates
 * in is never announced: it was not seen in flight.
 */
export function useCompletedAnnouncement(messages) {
  const [text, setText] = useState('');
  const inFlight = useRef(new Set());

  useEffect(() => {
    let announce = '';
    for (const message of messages || []) {
      if (message?.role !== 'assistant') continue;
      const key = message.request_id || message.id;
      if (message.status === 'pending' || message.status === 'streaming') {
        inFlight.current.add(key);
      } else if (inFlight.current.has(key) && (message.status === 'completed' || message.status === 'failed')) {
        inFlight.current.delete(key);
        announce = message.content ? `Neoh: ${message.content}` : 'Neoh replied.';
      }
    }
    if (!announce) return undefined;
    const frame = window.requestAnimationFrame(() => setText(announce));
    return () => window.cancelAnimationFrame(frame);
  }, [messages]);

  return text;
}
