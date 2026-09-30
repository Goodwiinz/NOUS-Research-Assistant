/**
 * Type aliases for the GOO-299 report/study identity HTTP boundary, sourced
 * directly from the generated OpenAPI contract
 * (`frontend/src/types/generated/api.d.ts`, regenerated via
 * `pnpm --dir frontend generate:api-types`). Never re-type these shapes.
 */
import type { components } from '@/types/generated/api';

export type ApiReport = components['schemas']['ReportResponse'];
export type ApiReportObservation =
  components['schemas']['ReportObservationResponse'];
export type ApiStudyLinkRequest = components['schemas']['StudyLinkRequest'];
export type ApiReportMergeRequest = components['schemas']['ReportMergeRequest'];
export type ApiIdentityEvent = components['schemas']['IdentityEventResponse'];
