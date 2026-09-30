import type { LocalTool } from "../mcp/server.ts";
import { ArtifactPathError, type ArtifactPublisher } from "./contracts.ts";

const uuid = (value: unknown): value is string =>
  typeof value === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);

/**
 * `artifacts_publish`: publish one file from the granted output root into the
 * NOUS artifact panel. Only the keys below are read from the model's
 * arguments; root, identity and provenance are injected locally.
 */
export function artifactsPublishTool(publisher: ArtifactPublisher): LocalTool {
  return {
    descriptor: {
      name: "artifacts_publish",
      description:
        "Publish a file from the registered local output folder as a NOUS artifact version. Reuse the same publication_id to retry safely.",
      inputSchema: {
        type: "object",
        additionalProperties: false,
        required: ["relative_path", "title", "publication_id"],
        properties: {
          relative_path: { type: "string", description: "Path relative to the registered output folder" },
          title: { type: "string", maxLength: 255 },
          publication_id: { type: "string", format: "uuid", description: "Client-generated UUID; identical retries return the same version" },
          artifact_id: { type: "string", format: "uuid" },
          expected_parent_version_id: { type: "string", format: "uuid" },
        },
      },
    },
    async call(args) {
      const relativePath = args.relative_path;
      const title = args.title;
      const publicationId = args.publication_id;
      if (typeof relativePath !== "string" || typeof title !== "string" || !title.trim() || !uuid(publicationId))
        return { text: "artifacts_publish requires relative_path, title and a UUID publication_id", isError: true };
      const artifactId = args.artifact_id;
      const expected = args.expected_parent_version_id;
      if ((artifactId !== undefined && !uuid(artifactId)) || (expected !== undefined && !uuid(expected)))
        return { text: "artifact_id and expected_parent_version_id must be UUIDs", isError: true };
      try {
        const version = await publisher.publish({
          relativePath,
          title: title.trim(),
          publicationId,
          ...(uuid(artifactId) ? { artifactId } : {}),
          ...(uuid(expected) ? { expectedParentVersionId: expected } : {}),
        });
        return { text: JSON.stringify(version), structured: version };
      } catch (error) {
        if (error instanceof ArtifactPathError)
          return { text: `${error.code}: ${error.message}`, isError: true };
        throw error;
      }
    },
  };
}
