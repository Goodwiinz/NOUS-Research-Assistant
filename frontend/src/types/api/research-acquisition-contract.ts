/**
 * Type aliases for the GOO-303 full-text acquisition and PRISMA flow HTTP
 * boundary, sourced directly from the generated OpenAPI contract
 * (`frontend/src/types/generated/api.d.ts`, regenerated via
 * `pnpm --dir frontend generate:api-types`). Never re-type these shapes.
 */
import type { components } from '@/types/generated/api';

type Schemas = components['schemas'];

export type ApiFulltextState = Schemas['FulltextStateResponse'];
export type ApiFulltextStatus = ApiFulltextState['state'];
export type ApiFulltextRequestCreate = Schemas['FulltextRequestCreate'];
export type ApiFulltextAttemptCreate = Schemas['FulltextAttemptCreate'];
export type ApiPrismaFlow = Schemas['PrismaFlowResponse'];
export type ApiPrismaCounts = Schemas['PrismaCounts'];
