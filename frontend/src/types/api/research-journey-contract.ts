/**
 * Type aliases for the GOO-308 plan-to-write journey HTTP boundary, sourced
 * directly from the generated OpenAPI contract
 * (`frontend/src/types/generated/api.d.ts`, regenerated via
 * `pnpm --dir frontend generate:api-types`). Never re-type these shapes.
 */
import type { components } from '@/types/generated/api';

type Schemas = components['schemas'];

export type ApiJourneyResponse = Schemas['JourneyResponse'];
export type ApiJourneyStage = Schemas['JourneyStage'];
export type ApiJourneyStageKey = ApiJourneyStage['key'];
export type ApiJourneyStatus = ApiJourneyStage['status'];
