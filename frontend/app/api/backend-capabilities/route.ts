import { resolveBackendUrl } from '../../../config/resolveBackendUrl';

const project = '/api/v1/projects/{project_id}';
const draft = `${project}/drafts/{draft_id}/versions/{version}`;
const unavailable = { draftClaims: false, draftRelease: false };

function hasOperation(
  paths: Record<string, unknown>,
  path: string,
  method: string
): boolean {
  const item = paths[path];
  if (!item || typeof item !== 'object' || Array.isArray(item)) return false;
  const operation = (item as Record<string, unknown>)[method];
  return Boolean(
    operation && typeof operation === 'object' && !Array.isArray(operation)
  );
}

/** Read the deployed API, never the frontend build's newer schema snapshot. */
export async function GET(): Promise<Response> {
  const headers = { 'Cache-Control': 'no-store' };
  try {
    const response = await fetch(`${resolveBackendUrl()}/openapi.json`, {
      cache: 'no-store',
      signal: AbortSignal.timeout(3000),
    });
    if (!response.ok) throw new Error('Capability discovery unavailable');
    const schema: unknown = await response.json();
    const paths =
      schema && typeof schema === 'object' && 'paths' in schema
        ? schema.paths
        : null;
    if (!paths || typeof paths !== 'object' || Array.isArray(paths))
      throw new Error('Invalid API schema');
    const operations = paths as Record<string, unknown>;
    return Response.json(
      {
        draftClaims:
          hasOperation(operations, `${project}/claims`, 'get') &&
          hasOperation(operations, `${project}/claims/export`, 'get'),
        draftRelease:
          hasOperation(operations, `${draft}/release`, 'get') &&
          hasOperation(operations, `${draft}/promote`, 'post') &&
          hasOperation(
            operations,
            '/api/v1/research-engine/projects/{project_id}/roles',
            'get'
          ),
      },
      { headers }
    );
  } catch {
    return Response.json(unavailable, { status: 503, headers });
  }
}
