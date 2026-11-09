// design-sync: `process.env` for the browser bundle. Next inlines
// process.env.NEXT_PUBLIC_* at build time; the design-sync bundle is plain
// esbuild (only NODE_ENV is defined), so app modules that read env at import
// time throw `process is not defined`. Listed FIRST in cfg.extraEntries so it
// evaluates before any app module.
//
// Every backend URL points at the reserved `.invalid` TLD (RFC 2606), which
// never resolves: designs and previews can never reach a real NOUS backend or
// Supabase project, and data hooks settle into their offline/error states.
// Supabase's createClient rejects empty values, so placeholders are non-empty.
const OFFLINE = 'https://design-preview.invalid';

const g = globalThis as unknown as { process?: { env: Record<string, string | undefined> } };
g.process ??= { env: {} };
Object.assign(g.process.env, {
  NODE_ENV: 'development',
  NEXT_PUBLIC_APP_ENV: 'design-preview',
  NEXT_PUBLIC_API_URL: OFFLINE,
  NEXT_PUBLIC_API_BASE_URL: OFFLINE,
  NEXT_PUBLIC_FRONTEND_URL: OFFLINE,
  NEXT_PUBLIC_APP_URL: OFFLINE,
  NEXT_PUBLIC_WS_URL: 'wss://design-preview.invalid',
  NEXT_PUBLIC_WEBSOCKET_URL: 'wss://design-preview.invalid',
  NEXT_PUBLIC_SUPABASE_URL: OFFLINE,
  NEXT_PUBLIC_SUPABASE_ANON_KEY: 'design-preview-anon-key',
});

export {};
