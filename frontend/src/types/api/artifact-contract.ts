/**
 * Wire shapes for the artifact routes, aliased from the generated OpenAPI
 * types (regenerate with `pnpm --dir frontend generate:api-types`). Domain
 * types live in `@/services/artifactService`.
 */
import type { components, operations } from '@/types/generated/api';

export type ApiArtifactVersion = components['schemas']['ArtifactVersionDTO'];
export type ApiArtifactReference =
  components['schemas']['ArtifactReferenceDTO'];
export type ApiThreadArtifact = components['schemas']['ThreadArtifactDTO'];
export type ApiArtifactProvenance = components['schemas']['ArtifactProvenance'];
export type ApiProjectArtifact = components['schemas']['ProjectArtifactDTO'];
/** Paging query of `GET /artifacts/projects/{project_id}` (AD-1). */
export type ApiProjectArtifactsQuery = NonNullable<
  operations['project_artifacts_api_v1_artifacts_projects__project_id__get']['parameters']['query']
>;

export type ApiArtifactCapabilities =
  components['schemas']['ArtifactCapabilitiesDTO'];
export type ApiArtifactEditRequest =
  components['schemas']['EditArtifactVersionRequest'];
