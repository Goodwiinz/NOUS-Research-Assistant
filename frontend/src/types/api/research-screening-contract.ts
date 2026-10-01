/**
 * Type aliases for the GOO-301/302 screening queue HTTP boundary, sourced directly
 * from the generated OpenAPI contract (`frontend/src/types/generated/api.d.ts`,
 * regenerated via `pnpm --dir frontend generate:api-types`). Never re-type
 * these shapes.
 */
import type { components } from '@/types/generated/api';

type Schemas = components['schemas'];

export type ApiScreeningQueue = Schemas['ScreeningQueueResponse'];
export type ApiScreeningQueueCreate = Schemas['ScreeningQueueCreate'];
export type ApiScreeningAssignment = Schemas['ScreeningAssignmentResponse'];
export type ApiScreeningAssignmentCreate = Schemas['ScreeningAssignmentCreate'];
export type ApiScreeningRevokeRequest = Schemas['ScreeningRevokeRequest'];
export type ApiScreeningObservation = Schemas['ScreeningObservationResponse'];
export type ApiScreeningObservationCreate =
  Schemas['ScreeningObservationCreate'];
export type ApiMyScreeningQueue = Schemas['MyScreeningQueueResponse'];
export type ApiMyScreeningItem = Schemas['MyScreeningQueueItem'];
export type ApiScreeningDecision = ApiScreeningObservationCreate['decision'];
// GOO-302: blind reveal and adjudication.
export type ApiScreeningResolution = Schemas['ScreeningResolutionResponse'];
export type ApiScreeningConflict = Schemas['ScreeningConflictResponse'];
export type ApiScreeningAdjudicateRequest =
  Schemas['ScreeningAdjudicateRequest'];
export type ApiScreeningReopenRequest = Schemas['ScreeningReopenRequest'];
export type ApiScreeningEvent = Schemas['ScreeningEventResponse'];
