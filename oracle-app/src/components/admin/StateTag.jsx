import { stateWords, toneGlyph, toneOf } from './operatorModel';
import styles from './OperatorOverview.module.css';

// A state in words, with a glyph whose SHAPE also differs by tone — the
// colour is a third cue, never the only one.
export default function StateTag({ state, words }) {
  const tone = toneOf(state);
  return (
    <span className={styles.tag} data-tone={tone}>
      <span className={styles.glyph} aria-hidden="true">{toneGlyph(tone)}</span>
      {words || stateWords(state)}
    </span>
  );
}
