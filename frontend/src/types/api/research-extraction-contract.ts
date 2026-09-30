/**
 * Type aliases for the GOO-304 extraction form/observation HTTP boundary,
 * sourced directly from the generated OpenAPI contract
 * (`frontend/src/types/generated/api.d.ts`, regenerated via
 * `pnpm --dir frontend generate:api-types`). Never re-type these shapes.
 */
import type { components } from '@/types/generated/api';

type Schemas = components['schemas'];

export type ApiExtractionFormVersion = Schemas['ExtractionFormVersionResponse'];
export type ApiExtractionObservation = Schemas['ExtractionObservationResponse'];
export type ApiExtractionObservationCreate =
  Schemas['ExtractionObservationCreate'];
export type ApiExtractionAcceptedValue =
  Schemas['ExtractionAcceptedValueResponse'];
export type ApiExtractionAcceptCreate = Schemas['ExtractionAcceptCreate'];
export type ApiExtractionCellObservations =
  Schemas['ExtractionCellObservationsResponse'];
/**
 * `GET /matrices/{id}` is an untyped dict; its `form_version` key is the
 * service's version summary: the version response minus matrix/creator ids,
 * with a nullable `created_at`.
 */
export type ApiExtractionFormVersionSummary = Omit<
  ApiExtractionFormVersion,
  'matrix_id' | 'created_by_id' | 'created_at'
> & { created_at: string | null };
