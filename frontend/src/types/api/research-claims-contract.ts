/**
 * Type aliases for the GOO-306 versioned claims HTTP boundary, sourced directly
 * from the generated OpenAPI contract (`frontend/src/types/generated/api.d.ts`,
 * regenerated via `pnpm --dir frontend generate:api-types`). Never re-type
 * these shapes.
 */
import type { components } from '@/types/generated/api';

type Schemas = components['schemas'];

export type ApiClaimListResponse = Schemas['ClaimListResponse'];
export type ApiClaimSummary = Schemas['ClaimSummary'];
export type ApiClaimLink = Schemas['ClaimLinkResponse'];
export type ApiClaimLinkKind = ApiClaimLink['kind'];
