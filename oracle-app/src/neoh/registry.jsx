import {
  CallQueue,
  Comparison,
  EntityCard,
  Evidence,
  Metric,
  Timeline,
} from './primitives';
import { OpportunityBlock } from './primitives';
import styles from './registry.module.css';

/**
 * registry — the lookup from a primitive name to the thing that draws it.
 *
 * The generative half of this product generates ARRANGEMENT, never markup.
 * A model that could emit HTML could emit a fabricated number inside a
 * heading, and once rendered that is indistinguishable from a real one. So
 * the backend returns `{primitive, props}` drawn from a closed list, and this
 * file is the only place that decides what those become on screen.
 *
 * A primitive this file does not know is skipped with one console warning —
 * never a blank panel, and never a thrown error that takes the rest of the
 * answer down with it. A version skew between the two halves must cost one
 * block, not the screen.
 */

export const REGISTRY = Object.freeze({
  person: (props, ctx) => <EntityCard kind="person" {...props} onOpen={ctx.onOpen} />,
  property: (props, ctx) => <EntityCard kind="property" {...props} onOpen={ctx.onOpen} />,
  deal: (props, ctx) => (
    <EntityCard
      kind="deal"
      {...props}
      openCount={props.open_count}
      totalCount={props.total_count}
      onOpen={ctx.onOpen}
    />
  ),
  call_queue: (props, ctx) => <CallQueue {...props} onOpen={ctx.onOpen} onAct={ctx.onAct} />,
  comparison: (props, ctx) => <Comparison {...props} onOpen={ctx.onOpen} />,
  metric: (props) => <Metric {...props} />,
  timeline: (props) => <Timeline {...props} />,
  evidence: (props) => <Evidence {...props} />,
  opportunity: (props, ctx) => (
    <OpportunityBlock opportunity={props.opportunity} rank={props.rank} onDecided={ctx.onDecided} />
  ),
});

/** Every primitive the backend may send. A test pins both sides to this list. */
export const KNOWN_PRIMITIVES = Object.freeze(Object.keys(REGISTRY));

const warned = new Set();

/**
 * Draw one block. An unknown primitive is skipped with a single warning per
 * name, so a backend ahead of this build degrades by one card.
 */
export function renderBlock(item, ctx = {}, key = 0) {
  const draw = item && REGISTRY[item.primitive];
  if (!draw) {
    if (item?.primitive && !warned.has(item.primitive)) {
      warned.add(item.primitive);
      console.warn(`[neoh] no renderer for primitive "${item.primitive}" — skipped`);
    }
    return null;
  }
  return (
    <div className={styles.block} key={`${item.primitive}-${key}`}>
      {draw(item.props || {}, ctx)}
    </div>
  );
}

export function renderBlocks(blocks, ctx = {}) {
  if (!Array.isArray(blocks)) return null;
  return blocks.map((item, i) => renderBlock(item, ctx, i)).filter(Boolean);
}
