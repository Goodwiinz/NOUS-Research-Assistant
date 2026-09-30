import { ReauthenticationRequired, ToolRequestRejected } from "../mcp/client.ts";
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
      if (typeof relativePath !== "string" || !relativePath.trim() || typeof title !== "string" || !title.trim() || !uuid(publicationId))
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
        if (error instanceof ToolRequestRejected || error instanceof ReauthenticationRequired)
          throw error; // the server maps these to stable tool errors
        // Network failure, 5xx or a malformed reply: the backend may or may not
        // have committed. Tell the model how to find out without duplicating.
        const reason = error instanceof Error ? error.message : String(error);
        console.error(`artifacts_publish outcome unknown: ${reason}`);
        return {
          text: `publication outcome unknown: ${reason}. Retry with the same publication_id ${publicationId}; NOUS returns the committed version or finishes it.`,
          isError: true,
        };
      }
    },
  };
}

/** Advertised in place of artifacts_publish when the bound root cannot be used. */
export function unavailablePublishTool(root: string, reason: string): LocalTool {
  return {
    descriptor: {
      name: "artifacts_publish",
      description: `Unavailable: the registered output folder ${root} cannot be used (${reason}). Re-register the folder, then re-run nous-harness mcp install.`,
      inputSchema: { type: "object", additionalProperties: false, properties: {} },
    },
    async call() {
      return {
        text: `artifacts_publish unavailable: ${reason}. Re-register ${root} with nous-harness workspace add and re-run nous-harness mcp install.`,
        isError: true,
      };
    },
  };
}
