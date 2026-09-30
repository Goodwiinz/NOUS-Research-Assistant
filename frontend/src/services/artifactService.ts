import { api } from '@/services/api-client';
import type {
  ApiArtifactReference,
  ApiArtifactVersion,
  ApiThreadArtifact,
} from '@/types/api/artifact-contract';

export type ArtifactProducer = 'harness' | 'nous' | 'user';

export interface ArtifactVersion {
  artifactId: string;
  versionId: string;
  parentVersionId: string | null;
  title: string;
  mimeType: string;
  byteSize: number;
  sha256: string;
  createdAt: string;
  producer: ArtifactProducer;
  sourceIds: string[];
}

export interface ArtifactReference {
  artifactId: string;
  versionId: string;
  runId: string | null;
  threadId: string | null;
  messageId: string | null;
}

export interface ThreadArtifact {
  version: ArtifactVersion;
  reference: ArtifactReference;
}

export function toArtifactVersion(dto: ApiArtifactVersion): ArtifactVersion {
  return {
    artifactId: dto.artifact_id,
    versionId: dto.version_id,
    parentVersionId: dto.parent_version_id ?? null,
    title: dto.title,
    mimeType: dto.mime_type,
    byteSize: dto.byte_size,
    sha256: dto.sha256,
    createdAt: dto.created_at,
    producer: dto.provenance.producer,
    sourceIds: dto.provenance.source_ids ?? [],
  };
}

function toArtifactReference(dto: ApiArtifactReference): ArtifactReference {
  return {
    artifactId: dto.artifact_id,
    versionId: dto.version_id,
    runId: dto.run_id ?? null,
    threadId: dto.thread_id ?? null,
    messageId: dto.message_id ?? null,
  };
}

export function toThreadArtifact(dto: ApiThreadArtifact): ThreadArtifact {
  return {
    version: toArtifactVersion(dto.version),
    reference: toArtifactReference(dto.reference),
  };
}

/** Relative API path for a version's bytes; always fetched with auth. */
export function artifactVersionContentPath(versionId: string): string {
  return `/artifacts/versions/${encodeURIComponent(versionId)}/content`;
}

export const artifactService = {
  async listThreadArtifacts(threadId: string): Promise<ThreadArtifact[]> {
    const rows = await api.get<ApiThreadArtifact[]>(
      `/artifacts/threads/${encodeURIComponent(threadId)}`
    );
    return rows.map(toThreadArtifact);
  },
  async listVersions(artifactId: string): Promise<ArtifactVersion[]> {
    const rows = await api.get<ApiArtifactVersion[]>(
      `/artifacts/${encodeURIComponent(artifactId)}/versions`
    );
    return rows.map(toArtifactVersion);
  },
  /** Bytes plus the served content type; the caller decides how to render. */
  async fetchVersionBlob(versionId: string): Promise<Blob> {
    return api.fetchBlob(artifactVersionContentPath(versionId));
  },
  downloadVersion(version: ArtifactVersion): Promise<void> {
    return api.download(
      artifactVersionContentPath(version.versionId),
      version.title
    );
  },
};
