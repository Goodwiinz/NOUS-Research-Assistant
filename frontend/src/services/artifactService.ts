import { api } from '@/services/api-client';
import type {
  ApiArtifactCapabilities,
  ApiArtifactEditRequest,
  ApiArtifactReference,
  ApiArtifactVersion,
  ApiProjectArtifact,
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

export interface ProjectArtifact {
  artifactId: string;
  kind: string;
  title: string;
  threadId: string | null;
  updatedAt: string;
  version: ArtifactVersion;
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

export function toProjectArtifact(dto: ApiProjectArtifact): ProjectArtifact {
  return {
    artifactId: dto.artifact_id,
    kind: dto.kind,
    title: dto.title,
    threadId: dto.thread_id ?? null,
    updatedAt: dto.updated_at,
    version: toArtifactVersion(dto.current_version),
  };
}

/** Relative API path for a version's bytes; always fetched with auth. */
export function artifactVersionContentPath(versionId: string): string {
  return `/artifacts/versions/${encodeURIComponent(versionId)}/content`;
}

export interface ArtifactCapabilities {
  editingEnabled: boolean;
  previewEnabled: boolean;
}
export interface ArtifactEdit {
  expectedParentVersionId: string;
  publicationId: string;
  text: string;
}

export const artifactService = {
  async capabilities(signal?: AbortSignal): Promise<ArtifactCapabilities> {
    const dto = await api.get<ApiArtifactCapabilities>(
      '/artifacts/capabilities',
      { signal }
    );
    return {
      editingEnabled: dto.editing_enabled === true,
      previewEnabled: dto.preview_enabled === true,
    };
  },
  async editVersion(
    artifactId: string,
    edit: ArtifactEdit
  ): Promise<ArtifactVersion> {
    const body: ApiArtifactEditRequest = {
      expected_parent_version_id: edit.expectedParentVersionId,
      publication_id: edit.publicationId,
      text: edit.text,
    };
    return toArtifactVersion(
      await api.post<ApiArtifactVersion>(
        `/artifacts/${encodeURIComponent(artifactId)}/edits`,
        body,
        { retries: 0 }
      )
    );
  },
  async listThreadArtifacts(
    threadId: string,
    signal?: AbortSignal
  ): Promise<ThreadArtifact[]> {
    const rows = await api.get<ApiThreadArtifact[]>(
      `/artifacts/threads/${encodeURIComponent(threadId)}`,
      { signal }
    );
    return rows.map(toThreadArtifact);
  },
  async listProjectArtifacts(
    projectId: string,
    signal?: AbortSignal
  ): Promise<ProjectArtifact[]> {
    const rows = await api.get<ApiProjectArtifact[]>(
      `/artifacts/projects/${encodeURIComponent(projectId)}`,
      { signal }
    );
    return rows.map(toProjectArtifact);
  },
  async listVersions(
    artifactId: string,
    signal?: AbortSignal
  ): Promise<ArtifactVersion[]> {
    const rows = await api.get<ApiArtifactVersion[]>(
      `/artifacts/${encodeURIComponent(artifactId)}/versions`,
      { signal }
    );
    return rows.map(toArtifactVersion);
  },
  /** Bytes plus the served content type; the caller decides how to render. */
  async fetchVersionBlob(
    versionId: string,
    signal?: AbortSignal
  ): Promise<Blob> {
    return api.fetchBlob(artifactVersionContentPath(versionId), signal);
  },
  downloadVersion(version: ArtifactVersion): Promise<void> {
    return api.download(
      artifactVersionContentPath(version.versionId),
      version.title
    );
  },
};
