import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  CLIENT_OWNER_STORAGE_KEY,
  clearClientOwner,
  readClientOwner,
  writeClientOwner,
} from '@/lib/client-owner';

describe('client owner stamp', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it('reads back the user id it wrote', () => {
    writeClientOwner('user-A');

    expect(readClientOwner()).toBe('user-A');
    expect(localStorage.getItem(CLIENT_OWNER_STORAGE_KEY)).toBe('user-A');
  });

  it('answers unknown when no stamp was ever written', () => {
    expect(readClientOwner()).toBeNull();
  });

  it('forgets the owner on clear', () => {
    writeClientOwner('user-A');
    clearClientOwner();

    expect(readClientOwner()).toBeNull();
    expect(localStorage.getItem(CLIENT_OWNER_STORAGE_KEY)).toBeNull();
  });

  it('treats an empty stamp as unknown', () => {
    localStorage.setItem(CLIENT_OWNER_STORAGE_KEY, '');

    expect(readClientOwner()).toBeNull();
  });

  it('answers unknown instead of throwing when storage denies the read', () => {
    localStorage.setItem(CLIENT_OWNER_STORAGE_KEY, 'user-A');
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('Storage access denied', 'SecurityError');
    });

    expect(readClientOwner()).toBeNull();
  });

  it('swallows write and clear failures', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('Quota exceeded', 'QuotaExceededError');
    });
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new DOMException('Storage access denied', 'SecurityError');
    });

    expect(() => writeClientOwner('user-A')).not.toThrow();
    expect(() => clearClientOwner()).not.toThrow();
  });

  it('is a no-op without a window (server render)', () => {
    vi.stubGlobal('window', undefined);

    expect(readClientOwner()).toBeNull();
    expect(() => writeClientOwner('user-A')).not.toThrow();
    expect(() => clearClientOwner()).not.toThrow();
  });
});
