/**
 * Type aliases for the GOO-315 manuscript release HTTP boundary, sourced
 * directly from the generated OpenAPI contract
 * (`frontend/src/types/generated/api.d.ts`, regenerated via
 * `pnpm --dir frontend generate:api-types`). Never re-type these shapes.
 */
import type { components } from '@/types/generated/api';

type Schemas = components['schemas'];

export type ApiManuscriptRelease = Schemas['ManuscriptReleaseResponse'];
export type ApiManuscriptReleaseList = Schemas['ManuscriptReleaseListResponse'];
export type ApiCandidateCreate = Schemas['CandidateCreate'];
export type ApiPromoteRequest = Schemas['PromoteRequest'];
export type ApiReleaseVerification = Schemas['ReleaseVerification'];
export type ApiCheckResult = Schemas['CheckResult'];
export type ApiCheckState = ApiCheckResult['state'];
export type ApiReferenceReport = Schemas['ReferenceReport'];
export type ApiReferenceFormat = ApiReferenceReport['format'];
