import { createHash } from "node:crypto";
import { extname } from "node:path";
import type {
  ArtifactApiClient,
  ArtifactPublisher,
  ArtifactVersionDTO,
  GrantedOutputRoot,
  PublishFileInput,
} from "./contracts.ts";
import { readSnapshot } from "./snapshot.ts";

const MIME_BY_EXTENSION: Record<string, string> = {
  ".md": "text/markdown",
  ".markdown": "text/markdown",
  ".txt": "text/plain",
  ".csv": "text/csv",
  ".json": "application/json",
  ".py": "text/x-python",
  ".js": "application/javascript",
  ".html": "text/html",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".pdf": "application/pdf",
};

/**
 * Publishes one validated file snapshot as an artifact version. The root
 * comes from the local binding at construction time; MCP arguments can only
 * choose a relative path, a title and the durable publication id.
 */
export function createArtifactPublisher(
  client: ArtifactApiClient,
  root: GrantedOutputRoot,
): ArtifactPublisher {
  async function publish(input: PublishFileInput): Promise<ArtifactVersionDTO> {
    const bytes = await readSnapshot(root, input.relativePath);
    const sha256 = createHash("sha256").update(bytes).digest("hex");
    const reservation = await client.reserve({
      publication_id: input.publicationId,
      byte_size: bytes.byteLength,
      mime_type: MIME_BY_EXTENSION[extname(input.relativePath).toLowerCase()] ?? "application/octet-stream",
      sha256,
    });
    await client.upload(reservation.upload_id, bytes);
    return client.finalize({
      publication_id: input.publicationId,
      upload_id: reservation.upload_id,
      title: input.title,
      // Reported by the harness; only server-observed identities are verified.
      provenance: { producer: "harness", source_ids: [] },
      ...(input.artifactId ? { artifact_id: input.artifactId } : {}),
      ...(input.expectedParentVersionId
        ? { expected_parent_version_id: input.expectedParentVersionId }
        : {}),
    });
  }
  return { publish };
}
