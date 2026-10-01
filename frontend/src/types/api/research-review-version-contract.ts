/**
 * Type aliases for the GOO-320 superseding review version HTTP boundary,
 * sourced directly from the generated OpenAPI contract
 * (`frontend/src/types/generated/api.d.ts`, regenerated via
 * `pnpm --dir frontend generate:api-types`). Never re-type these shapes.
 */
import type { components } from '@/types/generated/api';

export type ApiReviewVersionCreate =
  components['schemas']['ReviewVersionCreate'];
export type ApiReviewVersionList =
  components['schemas']['ReviewVersionListResponse'];
export type ApiReviewVersion = components['schemas']['ReviewVersionResponse'];
export type ApiReviewDeltaOption = components['schemas']['ReviewDeltaOption'];
export type ApiReviewWorkStatus = components['schemas']['ReviewWorkStatus'];
export type ApiReviewReleaseLinkCreate =
  components['schemas']['ReviewReleaseLinkCreate'];
export type ApiReviewReleaseLink =
  components['schemas']['ReviewReleaseLinkResponse'];
export type ApiUpdateAccounting =
  components['schemas']['UpdateAccountingResponse'];
