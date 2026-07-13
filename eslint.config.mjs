import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { FlatCompat } from '@eslint/eslintrc';

const dirname = path.dirname(fileURLToPath(import.meta.url));
const compat = new FlatCompat({ baseDirectory: dirname });

export default [
  {
    ignores: ['.next/**', 'node_modules/**'],
  },
  ...compat.extends('next/core-web-vitals'),
];
