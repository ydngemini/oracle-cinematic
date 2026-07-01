import { useRef, useCallback, useEffect } from 'react';

const TONE_CONFIGS = {
  warning: {
    frequency: 440,
    duration: 0.15,
    type: 'square',
    repeats: 2,
    volume: 0.3
  },
  critical: {
    frequency: 660,
    duration: 0.2,
    type: 'square',
    repeats: 3,
    volume: 0.4
  },
  breach: {
    frequency: 880,
    duration: 0.3,
    type: 'sawtooth',
    repeats: 4,
    volume: 0.5
  }
};

export default function useAudioAlertEngine() {
  const audioContextRef = useRef(null);
  const isPlayingRef = useRef(false);

  const getAudioContext = useCallback(() => {
    if (!audioContextRef.current) {
      audioContextRef.current = new (window.AudioContext || window.webkitAudioContext)();
    }
    return audioContextRef.current;
  }, []);

  const playTone = useCallback((config) => {
    if (isPlayingRef.current) return;
    
    const ctx = getAudioContext();
    if (ctx.state === 'suspended') {
      ctx.resume();
    }

    isPlayingRef.current = true;

    const playBeep = (repeatIndex) => {
      if (repeatIndex >= config.repeats) {
        isPlayingRef.current = false;
        return;
      }

      const oscillator = ctx.createOscillator();
      const gainNode = ctx.createGain();

      oscillator.type = config.type;
      oscillator.frequency.setValueAtTime(config.frequency, ctx.currentTime);

      const attackTime = 0.01;
      const releaseTime = config.duration * 0.3;

      gainNode.gain.setValueAtTime(0, ctx.currentTime);
      gainNode.gain.linearRampToValueAtTime(config.volume, ctx.currentTime + attackTime);
      gainNode.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + config.duration);

      oscillator.connect(gainNode);
      gainNode.connect(ctx.destination);

      oscillator.start(ctx.currentTime);
      oscillator.stop(ctx.currentTime + config.duration);

      setTimeout(() => playBeep(repeatIndex + 1), config.duration * 1000 + 50);
    };

    playBeep(0);
  }, [getAudioContext]);

  const playWarning = useCallback(() => {
    playTone(TONE_CONFIGS.warning);
  }, [playTone]);

  const playCritical = useCallback(() => {
    playTone(TONE_CONFIGS.critical);
  }, [playTone]);

  const playBreach = useCallback(() => {
    playTone(TONE_CONFIGS.breach);
  }, [playTone]);

  const playSeverity = useCallback((severity) => {
    const config = TONE_CONFIGS[severity];
    if (config) {
      playTone(config);
    }
  }, [playTone]);

  const stopAll = useCallback(() => {
    if (audioContextRef.current) {
      audioContextRef.current.close();
      audioContextRef.current = null;
    }
    isPlayingRef.current = false;
  }, []);

  useEffect(() => {
    return () => stopAll();
  }, [stopAll]);

  return {
    playWarning,
    playCritical,
    playBreach,
    playSeverity,
    stopAll,
    getAudioContext
  };
}

export { TONE_CONFIGS };
