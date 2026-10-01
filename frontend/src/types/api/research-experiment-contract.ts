/**
 * Type aliases for the GOO-312 run-manifest, artifact and figure HTTP
 * boundary, sourced directly from the generated OpenAPI contract
 * (`frontend/src/types/generated/api.d.ts`, regenerated via
 * `pnpm --dir frontend generate:api-types`). Never re-type these shapes.
 */
import type { components } from '@/types/generated/api';

type Schemas = components['schemas'];

export type ApiRunManifestV2 = Schemas['RunManifestV2Response'];
export type ApiRunArtifact = Schemas['RunArtifactResponse'];
export type ApiFigureCreate = Schemas['FigureCreate'];
export type ApiFigure = Schemas['FigureResponse'];
export type ApiFigureList = Schemas['FigureListResponse'];
export type ApiFigureLineage = Schemas['FigureLineageResponse'];
