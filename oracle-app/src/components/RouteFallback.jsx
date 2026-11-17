import styles from './RouteFallback.module.css';

/** Suspense fallback for lazy route chunks: visible and announced by screen readers. */
export default function RouteFallback() {
  return (
    <div className={styles.root} role="status" aria-live="polite">
      Loading…
    </div>
  );
}
