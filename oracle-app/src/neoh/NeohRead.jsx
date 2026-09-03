import styles from './EntitySheet.module.css';

/**
 * Neoh's read — one sentence, a confidence, and when the graph holds two
 * things that cannot both be true, the question that resolves it. The first
 * thing on every entity sheet. It never renders a verdict below the
 * confidence floor; it says the read is still forming instead.
 */
export function NeohRead({ read, loading = false, error = null }) {
  if (loading && !read) {
    return <div className={styles.read} aria-busy="true"><span className={styles.readGhost}>Reading…</span></div>;
  }
  if (error && !read) {
    return <div className={styles.read}><span className={styles.readGhost}>Neoh has no read on this yet.</span></div>;
  }
  if (!read) return null;
  const pct = Math.round((read.confidence || 0) * 100);
  return (
    <div className={styles.read}>
      <span className={styles.readKicker}>Neoh's read</span>
      <p className={`${styles.readSentence} ${read.forming ? styles.readForming : ''}`}>
        {read.forming ? `Still forming — ${read.sentence}` : read.sentence}
      </p>
      <span className={styles.readMeta}>
        {read.meta && <span>{read.meta}</span>}
        {read.confidence > 0 && read.confidence < 1 && (
          <span title="How sure Neoh is" aria-label={`Confidence ${pct} percent`}>
            <span className={styles.meter} aria-hidden="true">
              <span className={styles.meterFill} style={{ width: `${pct}%` }} />
            </span>
            {pct}%
          </span>
        )}
        {read.disputes > 0 && <span>{read.disputes} in dispute</span>}
      </span>
      {read.question && (
        <p className={styles.readQuestion}>
          <span className={styles.readKicker}>Worth asking</span>
          {read.question}
        </p>
      )}
    </div>
  );
}

export default NeohRead;
