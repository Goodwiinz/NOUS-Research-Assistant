/**
 * Type aliases for the GOO-313 fresh-rerun HTTP boundary, sourced directly
 * from the generated OpenAPI contract (`frontend/src/types/generated/api.d.ts`,
 * regenerated via `pnpm --dir frontend generate:api-types`). Never re-type
 * these shapes.
 */
import type { components } from '@/types/generated/api';

type Schemas = components['schemas'];

export type ApiRerunCreate = Schemas['RerunCreate'];
export type ApiRerunEligibility = Schemas['RerunEligibilityResponse'];
export type ApiRerun = Schemas['RerunResponse'];
export type ApiRerunList = Schemas['RerunListResponse'];
export type ApiRerunAttempt = Schemas['RerunAttemptResponse'];
export type ApiRerunComparisonRow = Schemas['RerunComparisonRow'];
