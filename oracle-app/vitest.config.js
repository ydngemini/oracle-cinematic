import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * Test config, deliberately separate from vite.config.js.
 *
 * The build config emits the service worker via a buildStart hook and points
 * envDir at the repo root; neither is wanted under test, and the SW emit throws
 * when there is no build to attach to. Sharing one config would mean guarding
 * both concerns with `mode === 'test'` checks in the build path.
 */
export default defineConfig({
  plugins: [react()],
  test: {
    // jsdom only where a test asks for it (`@vitest-environment jsdom`), so pure
    // logic tests keep node's much faster startup.
    environment: 'node',
    include: ['src/**/*.{test,spec}.{js,jsx,ts,tsx}'],
    css: false,
    restoreMocks: true,
    // This development host also runs local AI workloads. The previous
    // unbounded worker pool caused unrelated jsdom renders to starve each
    // other and exceed Vitest's 5s default (observed at 8--28s). A single,
    // deterministic worker makes a real regression reproducible instead of
    // randomly failing another feature's verification cycle.
    pool: 'forks',
    fileParallelism: false,
    maxWorkers: 1,
    testTimeout: 30_000,
    hookTimeout: 30_000,
  },
})
