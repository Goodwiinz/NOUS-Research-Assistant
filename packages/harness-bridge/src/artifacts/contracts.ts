import type { components } from "../../../../frontend/src/types/generated/api.d.ts";

export type ArtifactProvenance = components["schemas"]["ArtifactProvenance"];
export type ReserveArtifactUploadRequest =
  components["schemas"]["ReserveArtifactUploadRequest"];
export type PublishVersionRequest = components["schemas"]["PublishVersionRequest"];
export type ArtifactUploadDTO = components["schemas"]["ArtifactUploadDTO"];
export type ArtifactVersionDTO = components["schemas"]["ArtifactVersionDTO"];

/** Local publish input; identity, root and provenance are injected, never model-supplied. */
export type PublishFileInput = {
  relativePath: string;
  title: string;
  publicationId: string;
  artifactId?: string;
  expectedParentVersionId?: string;
};
/** The registered output root, pinned by device and inode at binding time. */
export type GrantedOutputRoot = { path: string; device: number; inode: number };

export interface ArtifactApiClient {
  reserve(request: ReserveArtifactUploadRequest): Promise<ArtifactUploadDTO>;
  upload(uploadId: string, bytes: Uint8Array): Promise<void>;
  finalize(request: PublishVersionRequest): Promise<ArtifactVersionDTO>;
}
export interface ArtifactPublisher {
  publish(input: PublishFileInput): Promise<ArtifactVersionDTO>;
}

export class ArtifactPathError extends Error {
  readonly code: "unsafe_path" | "too_large" | "not_found";
  constructor(code: "unsafe_path" | "too_large" | "not_found", message: string) {
    super(message);
    this.code = code;
  }
}
