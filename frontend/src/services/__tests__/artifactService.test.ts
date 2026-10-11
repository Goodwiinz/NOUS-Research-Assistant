import { describe, expect, it, vi } from 'vitest';

vi.mock('@/services/api-client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
    fetchBlob: vi.fn(),
    download: vi.fn(),
  },
}));

import { api } from '@/services/api-client';
import {
  artifactService,
  artifactVersionContentPath,
  toThreadArtifact,
} from '@/services/artifactService';
import type { ApiThreadArtifact } from '@/types/api/artifact-contract';

const wire: ApiThreadArtifact = {
  version: {
    artifact_id: 'a1',
    version_id: 'v1',
    parent_version_id: null,
    title: 'report.md',
    mime_type: 'text/markdown',
    byte_size: 7,
    sha256: 'abc',
    created_at: '2026-09-30T00:00:00Z',
    provenance: { producer: 'harness', source_ids: ['d1'] },
  },
  reference: {
    artifact_id: 'a1',
    version_id: 'v1',
    run_id: 'r1',
    thread_id: 't1',
    message_id: null,
  },
};

describe('artifactService', () => {
  it('maps wire shapes to camelCase domain types', () => {
    const item = toThreadArtifact(wire);
    expect(item.version).toEqual({
      artifactId: 'a1',
      versionId: 'v1',
      parentVersionId: null,
      title: 'report.md',
      mimeType: 'text/markdown',
      byteSize: 7,
      sha256: 'abc',
      createdAt: '2026-09-30T00:00:00Z',
      producer: 'harness',
      sourceIds: ['d1'],
    });
    expect(item.reference).toEqual({
      artifactId: 'a1',
      versionId: 'v1',
      runId: 'r1',
      threadId: 't1',
      messageId: null,
    });
  });

  it('calls the thread and version routes with encoded ids', async () => {
    vi.mocked(api.get).mockResolvedValueOnce([wire]);
    const items = await artifactService.listThreadArtifacts('t 1');
    expect(api.get).toHaveBeenCalledWith('/artifacts/threads/t%201', {
      signal: undefined,
    });
    expect(items[0]?.version.versionId).toBe('v1');
    vi.mocked(api.get).mockResolvedValueOnce([wire.version]);
    await artifactService.listVersions('a/1');
    expect(api.get).toHaveBeenCalledWith('/artifacts/a%2F1/versions', {
      signal: undefined,
    });
    expect(artifactVersionContentPath('v1')).toBe(
      '/artifacts/versions/v1/content'
    );
  });

  it('downloads through the authenticated client with the artifact title', async () => {
    await artifactService.downloadVersion(toThreadArtifact(wire).version);
    expect(api.download).toHaveBeenCalledWith(
      '/artifacts/versions/v1/content',
      'report.md'
    );
  });
});

it('adopts the generated browser edit request and disables automatic POST retries', async () => {
  vi.mocked(api.post).mockResolvedValue(wire.version);
  await artifactService.editVersion('a/1', {
    expectedParentVersionId: 'v1',
    publicationId: 'receipt',
    text: '',
  });
  expect(api.post).toHaveBeenCalledWith(
    '/artifacts/a%2F1/edits',
    { expected_parent_version_id: 'v1', publication_id: 'receipt', text: '' },
    { retries: 0 }
  );
});
it('requires exact true capabilities and forwards request cancellation', async () => {
  vi.mocked(api.get).mockResolvedValue({
    editing_enabled: 'true',
    preview_enabled: null,
  });
  const signal = new AbortController().signal;
  expect(await artifactService.capabilities(signal)).toEqual({
    editingEnabled: false,
    previewEnabled: false,
    sharingEnabled: false,
  });
  expect(api.get).toHaveBeenCalledWith('/artifacts/capabilities', { signal });
});

it('creates an exact-version audience share and maps only safe shared metadata', async () => {
  vi.mocked(api.post).mockResolvedValue({
    id: 's1',
    version_id: 'v1',
    expires_at: '2026-10-12T12:00:00Z',
    revoked_at: null,
  });
  const share = await artifactService.createArtifactShare(
    'v1',
    ['recipient'],
    '2026-10-12T12:00:00Z'
  );
  expect(api.post).toHaveBeenCalledWith(
    '/artifacts/shares',
    {
      version_id: 'v1',
      audience_user_ids: ['recipient'],
      expires_at: '2026-10-12T12:00:00Z',
    },
    { retries: 0, signal: undefined }
  );
  expect(share).toEqual({
    id: 's1',
    versionId: 'v1',
    expiresAt: '2026-10-12T12:00:00Z',
    revokedAt: null,
  });
  vi.mocked(api.get).mockResolvedValue({
    artifact_id: 'a1',
    version_id: 'v1',
    title: 'report.md',
    mime_type: 'text/markdown',
    byte_size: 7,
    sha256: 'abc',
    provenance: { source_ids: ['private'] },
  });
  const signal = new AbortController().signal;
  expect(await artifactService.getArtifactShare('s/1', signal)).toEqual({
    artifactId: 'a1',
    versionId: 'v1',
    title: 'report.md',
    mimeType: 'text/markdown',
    byteSize: 7,
    sha256: 'abc',
  });
  expect(api.get).toHaveBeenCalledWith('/artifacts/shares/s%2F1', { signal });
  await artifactService.getSharedArtifactContent('s/1', signal);
  expect(api.fetchBlob).toHaveBeenCalledWith(
    '/artifacts/shares/s%2F1/content',
    signal
  );
  await artifactService.revokeArtifactShare('s/1');
  expect(api.delete).toHaveBeenCalledWith('/artifacts/shares/s%2F1', {
    retries: 0,
    signal: undefined,
  });
});

it('loads owner share history and fails closed when sharing capability is absent', async () => {
  vi.mocked(api.get).mockResolvedValueOnce([
    {
      id: 's1',
      version_id: 'v1',
      expires_at: '2026-10-12T12:00:00Z',
      revoked_at: null,
    },
  ]);
  expect(await artifactService.listArtifactShares('a/1')).toEqual([
    {
      id: 's1',
      versionId: 'v1',
      expiresAt: '2026-10-12T12:00:00Z',
      revokedAt: null,
    },
  ]);
  expect(api.get).toHaveBeenCalledWith('/artifacts/a%2F1/shares', {
    signal: undefined,
  });
  vi.mocked(api.get).mockResolvedValueOnce({
    editing_enabled: true,
    preview_enabled: true,
    sharing_enabled: true,
  });
  expect((await artifactService.capabilities()).sharingEnabled).toBe(true);
});
