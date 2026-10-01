/**
 * Type aliases for the GOO-311 quantitative synthesis HTTP boundary, sourced
 * directly from the generated OpenAPI contract
 * (`frontend/src/types/generated/api.d.ts`, regenerated via
 * `pnpm --dir frontend generate:api-types`). Never re-type these shapes.
 */
import type { components } from '@/types/generated/api';

type Schemas = components['schemas'];

export type ApiSynthesisList = Schemas['SynthesisListResponse'];
export type ApiSynthesisSelection = Schemas['SynthesisSelection'];
export type ApiSynthesisPreview = Schemas['SynthesisPreview'];
export type ApiSynthesisExecute = Schemas['SynthesisExecute'];
export type ApiSynthesisRoles = Schemas['SynthesisRoles'];
export type ApiSynthesisResult = Schemas['SynthesisResultResponse'];
export type ApiSynthesisIncluded = Schemas['SynthesisIncluded'];
export type ApiSynthesisExclusion = Schemas['SynthesisExclusion'];
