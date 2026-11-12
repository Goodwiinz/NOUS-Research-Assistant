import { describe, expect, it } from 'vitest';

import { metadata as authLayout } from '../../../../app/(auth)/layout';
import { metadata as cliAuth } from '../../../../app/(auth)/cli-auth/layout';
import { metadata as forgotPassword } from '../../../../app/(auth)/forgot-password/layout';
import { metadata as login } from '../../../../app/(auth)/login/layout';
import { metadata as register } from '../../../../app/(auth)/register/layout';
import { metadata as resetPassword } from '../../../../app/(auth)/reset-password/layout';
import { metadata as verifyEmail } from '../../../../app/(auth)/verify-email/layout';
import { metadata as arxivLayout } from '../../../../app/(public)/arxiv/layout';
import { metadata as arxivPage } from '../../../../app/(public)/arxiv/page';

describe('public route tab titles', () => {
  it('keeps the auth template and gives it a sentence-case default', () => {
    expect(authLayout.title).toEqual({
      template: '%s | NOUS',
      default: 'Sign in | NOUS',
    });
  });

  it.each([
    ['login', login, 'Sign in'],
    ['register', register, 'Create account'],
    ['forgot-password', forgotPassword, 'Reset password'],
    ['reset-password', resetPassword, 'Choose a new password'],
    ['verify-email', verifyEmail, 'Verify email'],
    ['cli-auth', cliAuth, 'Connect the CLI'],
  ])('gives /%s its own title', (_route, meta, title) => {
    expect(meta.title).toBe(title);
  });

  it('titles /arxiv the same way in its layout and page', () => {
    expect(arxivLayout.title).toBe('arXiv | NOUS');
    expect(arxivPage.title).toBe('arXiv | NOUS');
  });
});
