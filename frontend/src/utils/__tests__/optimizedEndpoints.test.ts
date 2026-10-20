// @vitest-environment node
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { runInNewContext } from 'node:vm';
import { describe, expect, it } from 'vitest';

const require = createRequire(import.meta.url);
const { resolveBackendUrl } = require('../../../config/resolveBackendUrl');
const configPath = resolve(
  import.meta.dirname,
  '../../../next.optimized.config.js'
);

type OptimizedConfig = {
  env: Record<string, string>;
  rewrites: () => Promise<Array<{ source: string; destination: string }>>;
};

function loadConfig(env: Record<string, string>): OptimizedConfig {
  const loadedModule = { exports: {} };
  runInNewContext(readFileSync(configPath, 'utf8'), {
    module: loadedModule,
    process: { env },
    require: (name: string) => {
      if (name === '@next/bundle-analyzer')
        return () => (config: unknown) => config;
      if (name === './config/resolveBackendUrl') {
        return { resolveBackendUrl: () => resolveBackendUrl(env) };
      }
      if (name === 'webpack' || name === 'compression-webpack-plugin')
        return {};
      throw new Error(`Unexpected config dependency: ${name}`);
    },
  });
  return loadedModule.exports as OptimizedConfig;
}

describe('optimized build endpoint configuration', () => {
  it('leaves missing public endpoints unset in production output', () => {
    const config = loadConfig({
      NODE_ENV: 'production',
      BACKEND_URL: 'https://api.example',
    });
    expect(config.env).not.toHaveProperty('NEXT_PUBLIC_API_URL');
    expect(config.env).not.toHaveProperty('NEXT_PUBLIC_WS_URL');
  });

  it('uses the same server-only backend resolver as the standard config', async () => {
    const config = loadConfig({
      NODE_ENV: 'production',
      BACKEND_URL: 'https://api.example/',
    });
    expect(await config.rewrites()).toEqual([
      {
        source: '/api/v1/:path*',
        destination: 'https://api.example/api/v1/:path*',
      },
      {
        source: '/api/v2/:path*',
        destination: 'https://api.example/api/v2/:path*',
      },
    ]);
  });

  it('fails instead of silently rewriting production requests to localhost', async () => {
    const config = loadConfig({ NODE_ENV: 'production' });
    await expect(config.rewrites()).rejects.toThrow(/must be set/);
  });
});
