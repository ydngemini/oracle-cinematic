import { renderBlocks } from './registry';

/**
 * Blocks — a rendered answer, as a component rather than a function call.
 *
 * The distinction matters to more than the linter. Drawing the blocks by
 * calling a function during the parent's render meant the handler context
 * (which reaches for a ref to move focus) was built and read while the parent
 * was rendering. As a component the context is a prop, and the refs inside it
 * are only ever touched from the event handlers where they belong.
 */
export function Blocks({ blocks, ctx }) {
  return <>{renderBlocks(blocks, ctx)}</>;
}

export default Blocks;
