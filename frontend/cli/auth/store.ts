import {
  chmodSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from 'fs';
import { randomUUID } from 'node:crypto';
import * as os from 'os';
import * as path from 'path';

export interface NousConfig {
  token: string;
  user_email: string;
  organization_id: string;
  expires_at: string;
  thread_id: string | null;
  api_url?: string;
  model?: string;
  project_id?: string | null;
  project_name?: string | null;
  paper_id?: string | null;
  paper_title?: string | null;
}

function configDir(): string {
  return process.env.NOUS_CONFIG_DIR ?? path.join(os.homedir(), '.nous');
}

function configPath(): string {
  return path.join(configDir(), 'config.json');
}

export function loadConfig(): NousConfig | null {
  const p = configPath();
  if (!existsSync(p)) return null;
  try {
    chmodSync(configDir(), 0o700);
    chmodSync(p, 0o600);
    return JSON.parse(readFileSync(p, 'utf-8')) as NousConfig;
  } catch {
    return null;
  }
}

export function saveConfig(config: NousConfig): void {
  const dir = configDir();
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  chmodSync(dir, 0o700);
  const temporary = path.join(dir, `.config-${randomUUID()}.tmp`);
  try {
    writeFileSync(temporary, JSON.stringify(config, null, 2), {
      encoding: 'utf-8',
      mode: 0o600,
      flag: 'wx',
    });
    renameSync(temporary, configPath());
  } finally {
    rmSync(temporary, { force: true });
  }
}

export function clearConfig(): void {
  const p = configPath();
  if (existsSync(p)) rmSync(p);
}
