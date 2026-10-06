/**
 * Type aliases for the GOO-319 scheduled search update HTTP boundary,
 * sourced directly from the generated OpenAPI contract
 * (`frontend/src/types/generated/api.d.ts`, regenerated via
 * `pnpm --dir frontend generate:api-types`). Never re-type these shapes.
 */
import type { components } from '@/types/generated/api';

export type ApiSearchScheduleCreate =
  components['schemas']['SearchScheduleCreate'];
export type ApiSearchScheduleVersionCreate =
  components['schemas']['SearchScheduleVersionCreate'];
export type ApiSearchScheduleList =
  components['schemas']['SearchScheduleListResponse'];
export type ApiSearchSchedule = components['schemas']['SearchScheduleResponse'];
export type ApiSearchStrategyOption =
  components['schemas']['SearchStrategyOption'];
export type ApiSearchExecutionList =
  components['schemas']['SearchExecutionListResponse'];
export type ApiSearchExecution =
  components['schemas']['SearchExecutionResponse'];
export type ApiSearchDeltaExport = components['schemas']['SearchDeltaExport'];
export type ApiSearchDeltaItem = components['schemas']['SearchDeltaItem'];
