import type { BrowserContext } from '@playwright/test';
import { describe, expect, it, vi } from 'vitest';

import { loginForAgentQa } from '../../../../e2e/agent-qa/live-test';

describe('agent Q&A live login', () => {
  it('redacts a Playwright password-fill failure and closes the context', async () => {
    const password = 'synthetic-secret-do-not-report';
    const close = vi.fn().mockResolvedValue(undefined);
    const fill = vi
      .fn()
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(
        new Error(`locator.fill: value=${password}; element is disabled`)
      );
    const page = {
      goto: vi.fn().mockResolvedValue(undefined),
      getByTestId: vi.fn().mockReturnValue({ fill }),
    };
    const context = {
      newPage: vi.fn().mockResolvedValue(page),
      close,
    } as unknown as BrowserContext;

    let reportedError: unknown;
    try {
      await loginForAgentQa(context, 'https://example.test', {
        email: 'qa@example.test',
        password,
      });
    } catch (error) {
      reportedError = error;
    }

    expect(fill).toHaveBeenCalledWith('qa@example.test', { timeout: 30_000 });
    expect(fill).toHaveBeenCalledWith(password);
    expect(reportedError).toBeInstanceOf(Error);
    expect((reportedError as Error).message).toBe(
      'Agent Q&A login failed. Check credentials or login page availability.'
    );
    expect((reportedError as Error).cause).toBeUndefined();
    expect(String(reportedError)).not.toContain(password);
    expect((reportedError as Error).stack).not.toContain(password);
    expect(close).toHaveBeenCalledOnce();
  });
});
