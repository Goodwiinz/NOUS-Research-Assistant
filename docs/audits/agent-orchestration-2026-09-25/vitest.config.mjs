import { fileURLToPath } from 'node:url';

const auditDir = fileURLToPath(new URL('.', import.meta.url));
const repository = fileURLToPath(new URL('../../../', import.meta.url));

export default {
  root: auditDir,
  resolve: {
    alias: [
      { find: /^src\//, replacement: `${repository}frontend/src/` },
      { find: '@/nous', replacement: `${repository}frontend/app/components/nous` },
      { find: /^@\//, replacement: `${repository}frontend/src/` },
    ],
  },
  test: {
    environment: 'jsdom',
    setupFiles: [`${repository}frontend/src/test/setup.ts`],
    include: ['stale-approval.test.tsx'],
    testTimeout: 15000,
  },
};
