import { defineConfig, globalIgnores } from 'eslint/config';
import nextVitals from 'eslint-config-next/core-web-vitals';
import nextTypescript from 'eslint-config-next/typescript';
import noLoopbackRuntimeUrls from './eslint-rules/no-loopback-runtime-urls.mjs';

export default defineConfig([
  ...nextVitals,
  ...nextTypescript,
  {
    files: ['src/**/*.{ts,tsx,js,jsx}'],
    ignores: [
      'src/**/__tests__/**',
      'src/**/*.{test,spec}.{ts,tsx,js,jsx}',
      'src/test/**',
      // Intentional local development defaults belong in this resolver only.
      'src/utils/publicEndpoints.ts',
    ],
    plugins: {
      nous: { rules: { 'no-loopback-runtime-urls': noLoopbackRuntimeUrls } },
    },
    rules: { 'nous/no-loopback-runtime-urls': 'error' },
  },
  {
    rules: {
      'react/no-unescaped-entities': 'warn',
      '@typescript-eslint/no-explicit-any': 'warn',
      '@typescript-eslint/explicit-function-return-type': [
        'warn',
        {
          allowExpressions: true,
          allowTypedFunctionExpressions: true,
          allowHigherOrderFunctions: true,
          allowDirectConstAssertionInArrowFunctions: true,
        },
      ],
      '@typescript-eslint/no-unused-vars': [
        'warn',
        {
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^_',
        },
      ],
    },
  },
  globalIgnores([
    '.next/**',
    'out/**',
    'src/types/generated/**',
    'next-env.d.ts',
  ]),
]);
