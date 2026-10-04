/**
 * Type aliases for the GOO-307 draft release HTTP boundary, sourced directly
 * from the generated OpenAPI contract (`frontend/src/types/generated/api.d.ts`,
 * regenerated via `pnpm --dir frontend generate:api-types`). Never re-type
 * these shapes.
 */
import type { components } from '@/types/generated/api';

type Schemas = components['schemas'];

export type ApiReleaseCheck = Schemas['ReleaseCheckResponse'];
export type ApiReleaseStatus = ApiReleaseCheck['release_status'];
export type ApiReleaseBlocker = Schemas['ReleaseBlocker'];
export type ApiDraftRelease = Schemas['DraftReleaseResponse'];
export type ApiDraftPromoteRequest = Schemas['DraftPromoteRequest'];
