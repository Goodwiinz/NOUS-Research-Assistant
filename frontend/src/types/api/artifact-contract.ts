/**
 * Wire shapes for the artifact routes, aliased from the generated OpenAPI
 * types (regenerate with `pnpm --dir frontend generate:api-types`). Domain
 * types live in `@/services/artifactService`.
 */
import type { components } from '@/types/generated/api';

export type ApiArtifactVersion = components['schemas']['ArtifactVersionDTO'];
export type ApiArtifactReference =
  components['schemas']['ArtifactReferenceDTO'];
export type ApiThreadArtifact = components['schemas']['ThreadArtifactDTO'];
export type ApiArtifactProvenance = components['schemas']['ArtifactProvenance'];
export type ApiProjectArtifact = components['schemas']['ProjectArtifactDTO'];

export type ApiArtifactCapabilities =
  components['schemas']['ArtifactCapabilitiesDTO'];
export type ApiArtifactEditRequest =
  components['schemas']['EditArtifactVersionRequest'];
