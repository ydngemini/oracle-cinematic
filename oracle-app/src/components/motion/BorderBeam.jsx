import styles from './BorderBeam.module.css';

export function BorderBeam({
  className = '',
  size = 200,
  duration = 5,
  borderWidth = 1.5,
  colorFrom = '#b88952',
  colorTo = '#f4e5bc',
  delay = 0,
}) {
  const customProperties = {
    '--beam-size': `${Math.max(80, Number(size) || 200)}px`,
    '--beam-duration': `${Math.max(1, Number(duration) || 5)}s`,
    '--beam-border-width': `${Math.max(0.5, Number(borderWidth) || 1.5)}px`,
    '--beam-color-from': colorFrom,
    '--beam-color-to': colorTo,
    '--beam-delay': `${Number(delay) || 0}s`,
  };

  return (
    <span
      className={`${styles.borderBeam} ${className}`.trim()}
      style={customProperties}
      aria-hidden="true"
    >
      <span className={styles.orbit} />
    </span>
  );
}
