import { describe, expect, it, vi } from 'vitest';

vi.mock('@/services/api-client', () => ({
  api: { get: vi.fn(), fetchBlob: vi.fn(), download: vi.fn() },
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
  reference: { artifact_id: 'a1', version_id: 'v1', run_id: 'r1', thread_id: 't1', message_id: null },
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
    expect(item.reference).toEqual({ artifactId: 'a1', versionId: 'v1', runId: 'r1', threadId: 't1', messageId: null });
  });

  it('calls the thread and version routes with encoded ids', async () => {
    vi.mocked(api.get).mockResolvedValueOnce([wire]);
    const items = await artifactService.listThreadArtifacts('t 1');
    expect(api.get).toHaveBeenCalledWith('/artifacts/threads/t%201');
    expect(items[0]?.version.versionId).toBe('v1');
    vi.mocked(api.get).mockResolvedValueOnce([wire.version]);
    await artifactService.listVersions('a/1');
    expect(api.get).toHaveBeenCalledWith('/artifacts/a%2F1/versions');
    expect(artifactVersionContentPath('v1')).toBe('/artifacts/versions/v1/content');
  });

  it('downloads through the authenticated client with the artifact title', async () => {
    await artifactService.downloadVersion(toThreadArtifact(wire).version);
    expect(api.download).toHaveBeenCalledWith('/artifacts/versions/v1/content', 'report.md');
  });
});
