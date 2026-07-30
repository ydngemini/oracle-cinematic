import styles from './NeohBrandMark.module.css';

/**
 * Compact NEOH identity mark. The N is drawn as an architectural span above a
 * horizon line, with a single active node for the intelligence layer.
 */
export function NeohBrandMark() {
  return (
    <span className={styles.lockup}>
      <span className={styles.mark} aria-hidden="true">
        <svg viewBox="0 0 40 40" fill="none" focusable="false">
          <path className={styles.field} d="M5 10.5h30M5 20h30M5 29.5h30M10.5 5v30M20 5v30M29.5 5v30" />
          <path className={styles.horizon} d="M5 29.5h30" />
          <path className={styles.monogram} d="M10.5 29.5v-19l19 19v-19" />
          <path className={styles.facet} d="m10.5 10.5 4 4M29.5 29.5l-4-4" />
          <circle className={styles.node} cx="29.5" cy="10.5" r="2" />
        </svg>
      </span>
      <span className={styles.copy}>
        <span className={styles.name}>NEOH</span>
        <span className={styles.descriptor}>Property intelligence</span>
      </span>
    </span>
  );
}
