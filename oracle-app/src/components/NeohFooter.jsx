import styles from './NeohFooter.module.css';

export function NeohFooter() {
  const year = new Date().getFullYear();

  return (
    <footer className={styles.footer} data-neoh-footer="true" aria-label="NEOH™ legal notice">
      <span className={styles.mark}>
        <svg viewBox="0 0 20 20" fill="none" aria-hidden="true" focusable="false">
          <path d="M3 4.5h14M10 4.5v11" />
          <path d="m6.5 11 3.5 4 3.5-4" />
          <circle cx="10" cy="4.5" r="1.2" />
        </svg>
        <span>NEOH<sup>™</sup></span>
      </span>
      <span className={styles.copyright}>© {year} YDN LLC. All rights reserved.</span>
    </footer>
  );
}
