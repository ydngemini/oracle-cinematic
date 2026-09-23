// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { isSpeechInputSupported, speechErrorMessage, useSpeechInput } from './useSpeechInput';

/** A stand-in for the browser's SpeechRecognition, driven by the test. */
class FakeRecognition {
  static instances = [];
  constructor() {
    this.started = false;
    this.aborted = false;
    FakeRecognition.instances.push(this);
  }
  start() { this.started = true; this.onstart?.(); }
  stop() { this.onerror?.({ error: 'aborted' }); this.onend?.(); }
  abort() { this.aborted = true; }
  // Test helpers
  say(text, isFinal) {
    this.onresult?.({ resultIndex: 0, results: [Object.assign([{ transcript: text }], { isFinal })] });
  }
  fail(code) { this.onerror?.({ error: code }); this.onend?.(); }
}

beforeEach(() => {
  FakeRecognition.instances = [];
  window.SpeechRecognition = FakeRecognition;
});
afterEach(() => {
  delete window.SpeechRecognition;
  delete window.webkitSpeechRecognition;
  vi.restoreAllMocks();
});

describe('speech support detection', () => {
  it('reports supported when the browser has the API', () => {
    expect(isSpeechInputSupported()).toBe(true);
  });

  it('reports unsupported when it does not', () => {
    delete window.SpeechRecognition;
    expect(isSpeechInputSupported()).toBe(false);
    const { result } = renderHook(() => useSpeechInput({ onFinal: vi.fn() }));
    expect(result.current.supported).toBe(false);
    expect(result.current.state).toBe('unsupported');
  });

  it('accepts the webkit-prefixed name', () => {
    delete window.SpeechRecognition;
    window.webkitSpeechRecognition = FakeRecognition;
    expect(isSpeechInputSupported()).toBe(true);
  });
});

describe('useSpeechInput', () => {
  it('starts idle and does not touch the microphone until asked', () => {
    const { result } = renderHook(() => useSpeechInput({ onFinal: vi.fn() }));
    expect(result.current.state).toBe('idle');
    expect(FakeRecognition.instances).toHaveLength(0);
  });

  it('goes listening once the browser grants capture', () => {
    const { result } = renderHook(() => useSpeechInput({ onFinal: vi.fn() }));
    act(() => result.current.start());
    expect(result.current.state).toBe('listening');
  });

  it('captures one utterance per press, not a continuous stream', () => {
    renderHook(() => useSpeechInput({ onFinal: vi.fn() }));
    const { result } = renderHook(() => useSpeechInput({ onFinal: vi.fn() }));
    act(() => result.current.start());
    const rec = FakeRecognition.instances.at(-1);
    expect(rec.continuous).toBe(false);
    expect(rec.interimResults).toBe(true);
  });

  it('shows interim words without submitting them', () => {
    const onFinal = vi.fn();
    const { result } = renderHook(() => useSpeechInput({ onFinal }));
    act(() => result.current.start());
    act(() => FakeRecognition.instances.at(-1).say('who should I call', false));
    expect(result.current.interim).toBe('who should I call');
    expect(onFinal).not.toHaveBeenCalled();
  });

  it('hands a finished utterance back as plain text', () => {
    const onFinal = vi.fn();
    const { result } = renderHook(() => useSpeechInput({ onFinal }));
    act(() => result.current.start());
    act(() => FakeRecognition.instances.at(-1).say('  Call her.  ', true));
    expect(onFinal).toHaveBeenCalledWith('Call her.');
    expect(result.current.interim).toBe('');
  });

  it('settles back to idle so the button is usable again', () => {
    const { result } = renderHook(() => useSpeechInput({ onFinal: vi.fn() }));
    act(() => result.current.start());
    act(() => FakeRecognition.instances.at(-1).onend());
    expect(result.current.state).toBe('idle');
  });

  it('treats a deliberate stop as a stop, not an error', () => {
    const { result } = renderHook(() => useSpeechInput({ onFinal: vi.fn() }));
    act(() => result.current.start());
    act(() => result.current.stop());
    expect(result.current.state).toBe('idle');
    expect(result.current.error).toBe('');
  });

  it('pressing again while listening stops rather than stacking sessions', () => {
    const { result } = renderHook(() => useSpeechInput({ onFinal: vi.fn() }));
    act(() => result.current.start());
    act(() => result.current.start());
    expect(FakeRecognition.instances).toHaveLength(1);
    expect(result.current.state).toBe('idle');
  });

  it('explains a denied microphone in words a person can act on', () => {
    const { result } = renderHook(() => useSpeechInput({ onFinal: vi.fn() }));
    act(() => result.current.start());
    act(() => FakeRecognition.instances.at(-1).fail('not-allowed'));
    expect(result.current.state).toBe('error');
    expect(result.current.error).toMatch(/permission/i);
  });

  it('keeps the error visible until dismissed', () => {
    const { result } = renderHook(() => useSpeechInput({ onFinal: vi.fn() }));
    act(() => result.current.start());
    act(() => FakeRecognition.instances.at(-1).fail('audio-capture'));
    expect(result.current.state).toBe('error');
    act(() => result.current.clearError());
    expect(result.current.state).toBe('idle');
    expect(result.current.error).toBe('');
  });

  it('will not open the microphone while disabled', () => {
    const { result } = renderHook(() => useSpeechInput({ onFinal: vi.fn(), disabled: true }));
    act(() => result.current.start());
    expect(FakeRecognition.instances).toHaveLength(0);
    expect(result.current.state).toBe('idle');
  });

  it('aborts a live session when the surface unmounts', () => {
    const { result, unmount } = renderHook(() => useSpeechInput({ onFinal: vi.fn() }));
    act(() => result.current.start());
    const rec = FakeRecognition.instances.at(-1);
    unmount();
    expect(rec.aborted).toBe(true);
  });

  it('calls the newest onFinal, so a re-render cannot strand the callback', () => {
    const first = vi.fn();
    const second = vi.fn();
    const { result, rerender } = renderHook(({ cb }) => useSpeechInput({ onFinal: cb }), {
      initialProps: { cb: first },
    });
    act(() => result.current.start());
    rerender({ cb: second });
    act(() => FakeRecognition.instances.at(-1).say('hello', true));
    expect(second).toHaveBeenCalledWith('hello');
    expect(first).not.toHaveBeenCalled();
  });

  it('does not tear down the live session when the callback changes', () => {
    const { result, rerender } = renderHook(({ cb }) => useSpeechInput({ onFinal: cb }), {
      initialProps: { cb: vi.fn() },
    });
    act(() => result.current.start());
    rerender({ cb: vi.fn() });
    expect(FakeRecognition.instances).toHaveLength(1);
    expect(FakeRecognition.instances[0].aborted).toBe(false);
    expect(result.current.state).toBe('listening');
  });
});

describe('speechErrorMessage', () => {
  it.each([
    ['not-allowed', /permission/i],
    ['service-not-allowed', /permission/i],
    ['no-speech', /didn't hear/i],
    ['audio-capture', /no microphone/i],
    ['network', /connection/i],
    ['something-new', /unexpectedly/i],
  ])('%s reads as something actionable', (code, shape) => {
    expect(speechErrorMessage(code)).toMatch(shape);
  });
});
