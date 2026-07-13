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
    files: [
      'src/components/ClientShared.jsx',
      'src/components/DealPipeline.jsx',
      'src/components/MediaUploader.jsx',
      'src/state/StateContext.jsx',
    ],
    rules: {
      // These modules intentionally colocate reusable hooks/helpers with their
      // components. They are not hot-reload boundaries with hidden side effects.
      'react-refresh/only-export-components': 'off',
    },
  },
])
