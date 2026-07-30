import { useEffect, useRef, useState } from 'react';
import styles from './KineticText.module.css';

const DEFAULT_GLYPHS = '!@#$%^&*()_+-=[]{}|;:,.<>?/ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';

function randomGlyph(glyphs) {
  return glyphs[Math.floor(Math.random() * glyphs.length)] || '·';
}

function resolvedFrame(text) {
  const characters = Array.from(text);
  return { characters, resolvedCount: characters.length };
}

export function KineticText({
  text,
  speed = 40,
  scrambleSpeed = 30,
  glyphSet = DEFAULT_GLYPHS,
  className = '',
  onComplete,
}) {
  const safeText = String(text ?? '');
  const [frame, setFrame] = useState(() => resolvedFrame(safeText));
  const onCompleteRef = useRef(onComplete);

  useEffect(() => {
    onCompleteRef.current = onComplete;
  }, [onComplete]);

  useEffect(() => {
    let animationFrame = 0;
    let startedAt = null;
    let lastScrambleAt = Number.NEGATIVE_INFINITY;
    let previousResolvedCount = -1;
    let completed = false;

    const source = Array.from(safeText);
    const glyphs = glyphSet || DEFAULT_GLYPHS;
    const revealInterval = Math.max(10, Number(speed) || 40);
    const glyphInterval = Math.max(10, Number(scrambleSpeed) || 30);
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    const finish = () => {
      if (completed) return;
      completed = true;
      setFrame({ characters: source, resolvedCount: source.length });
      onCompleteRef.current?.();
    };

    if (reducedMotion || source.length === 0) {
      animationFrame = window.requestAnimationFrame(finish);
      return () => window.cancelAnimationFrame(animationFrame);
    }

    const animate = (timestamp) => {
      if (startedAt === null) startedAt = timestamp;
      const elapsed = timestamp - startedAt;
      const resolvedCount = Math.min(
        source.length,
        Math.floor(elapsed / revealInterval)
      );
      const shouldScramble = timestamp - lastScrambleAt >= glyphInterval;

      if (shouldScramble || resolvedCount !== previousResolvedCount) {
        if (shouldScramble) lastScrambleAt = timestamp;
        previousResolvedCount = resolvedCount;
        setFrame({
          resolvedCount,
          characters: source.map((character, index) => {
            if (index < resolvedCount || /\s/.test(character)) return character;
            return randomGlyph(glyphs);
          }),
        });
      }

      if (resolvedCount >= source.length) {
        finish();
        return;
      }
      animationFrame = window.requestAnimationFrame(animate);
    };

    animationFrame = window.requestAnimationFrame(animate);
    return () => {
      completed = true;
      window.cancelAnimationFrame(animationFrame);
    };
  }, [glyphSet, safeText, scrambleSpeed, speed]);

  return (
    <span
      className={`${styles.kineticText} ${className}`.trim()}
      role="text"
      aria-label={safeText}
    >
      <span className={styles.visualText} aria-hidden="true">
        {frame.characters.map((character, index) => (
          <span
            className={styles.character}
            data-resolved={index < frame.resolvedCount}
            key={`${index}-${safeText.length}`}
          >
            {character === ' ' ? '\u00A0' : character}
          </span>
        ))}
      </span>
    </span>
  );
}
