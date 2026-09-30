import { integrationHeaders, type IntegrationCredentials } from "../credentials.ts";
import { apiBase, throwForStatus } from "../mcp/client.ts";
import type {
  ArtifactApiClient,
  ArtifactUploadDTO,
  ArtifactVersionDTO,
  PublishVersionRequest,
  ReserveArtifactUploadRequest,
} from "./contracts.ts";

const uuid = (value: unknown): value is string =>
  typeof value === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);

/** Scoped HTTPS client for the artifact publication routes. Never retries. */
export class ArtifactHttpClient implements ArtifactApiClient {
  private readonly base: string;
  private readonly headers: Record<string, string>;
  private readonly fetchFn: typeof fetch;
  constructor(apiOrigin: string, credentials: IntegrationCredentials, fetchFn: typeof fetch = fetch) {
    this.base = apiBase(apiOrigin);
    this.headers = integrationHeaders(credentials);
    this.fetchFn = fetchFn;
  }
  async reserve(request: ReserveArtifactUploadRequest): Promise<ArtifactUploadDTO> {
    const data = await this.json("POST", "/artifacts/uploads", request);
    if (typeof data !== "object" || data === null || !uuid((data as ArtifactUploadDTO).upload_id))
      throw new Error("invalid NOUS upload reservation");
    return data as ArtifactUploadDTO;
  }
  async upload(uploadId: string, bytes: Uint8Array): Promise<void> {
    if (!uuid(uploadId)) throw new Error("invalid upload id");
    const response = await this.send(
      "PUT",
      `/artifacts/uploads/${uploadId}/content`,
      bytes,
      { "Content-Type": "application/octet-stream", "Content-Length": String(bytes.byteLength) },
    );
    await throwForStatus(response);
  }
  async finalize(request: PublishVersionRequest): Promise<ArtifactVersionDTO> {
    const data = await this.json("POST", "/artifacts/versions", request);
    if (typeof data !== "object" || data === null || !uuid((data as ArtifactVersionDTO).version_id))
      throw new Error("invalid NOUS artifact version");
    return data as ArtifactVersionDTO;
  }
  private async json(method: "POST", path: string, body: object): Promise<unknown> {
    const response = await this.send(method, path, JSON.stringify(body), {
      "Content-Type": "application/json",
    });
    await throwForStatus(response);
    try {
      return await response.json();
    } catch {
      throw new Error("invalid NOUS response");
    }
  }
  private async send(
    method: "POST" | "PUT",
    path: string,
    body: string | Uint8Array,
    extra: Record<string, string>,
  ): Promise<Response> {
    const url = this.base + path;
    try {
      return await this.fetchFn(url, {
        method,
        redirect: "error",
        headers: { ...this.headers, ...extra },
        // Uint8Array<ArrayBufferLike> is not assignable to BodyInit in TS 5.9.
        body: body as BodyInit,
        signal: AbortSignal.timeout(60_000),
      });
    } catch (error) {
      const cause =
        error instanceof Error && error.cause instanceof Error
          ? error.cause.message
          : error instanceof Error
            ? error.message
            : String(error);
      const wrapped = new Error(`NOUS request to ${url} failed: ${cause}`);
      console.error(wrapped.message);
      throw wrapped;
    }
  }
}
