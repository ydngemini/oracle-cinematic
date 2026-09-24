import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist', 'dev-dist']),
  {
    files: ['**/*.{js,jsx}'],
    extends: [
      js.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
  },
  {
    // Build and tooling config runs in Node, not the browser: `process.env` in
    // vite.config.js is correct there and only looked like an error because the
    // block above assumes browser globals everywhere. Left unfixed it was a
    // permanent lint error, which is worse than none — it trains you to read
    // "1 problem" as "clean" and hides the next real one.
    files: ['*.config.js', 'vite.config.js', 'vitest.config.js', 'scripts/**/*.js'],
    languageOptions: { globals: globals.node },
  },
  {
    files: [
      'src/components/ClientShared.jsx',
      'src/components/DealPipeline.jsx',
      'src/state/StateContext.jsx',
    ],
    rules: {
      // These modules intentionally colocate reusable hooks/helpers with their
      // components. They are not hot-reload boundaries with hidden side effects.
      'react-refresh/only-export-components': 'off',
    },
  },
])
