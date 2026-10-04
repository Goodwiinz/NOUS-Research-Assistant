/**
 * Type aliases for the GOO-310 evidence table, contradiction and certainty
 * HTTP boundary, sourced directly from the generated OpenAPI contract
 * (`frontend/src/types/generated/api.d.ts`, regenerated via
 * `pnpm --dir frontend generate:api-types`). Never re-type these shapes.
 */
import type { components } from '@/types/generated/api';

type Schemas = components['schemas'];

export type ApiEvidenceOutcomeList = Schemas['EvidenceOutcomeListResponse'];
export type ApiEvidenceOutcome = Schemas['EvidenceOutcome'];
export type ApiEvidenceTablePreview = Schemas['EvidenceTablePreview'];
export type ApiEvidenceTableCreate = Schemas['EvidenceTableCreate'];
export type ApiEvidenceTable = Schemas['EvidenceTableResponse'];
export type ApiEvidenceRow = Schemas['EvidenceRow'];
export type ApiEvidenceCell = Schemas['EvidenceCell'];
export type ApiEvidenceCellState = ApiEvidenceCell['state'];
export type ApiEvidenceTip = Schemas['EvidenceTip'];
export type ApiEvidenceUnreviewedCell = Schemas['EvidenceUnreviewedCell'];
export type ApiStanceSuggestionGroup = Schemas['StanceSuggestionGroup'];
export type ApiContradictionCreate = Schemas['ContradictionCreate'];
export type ApiContradiction = Schemas['ContradictionResponse'];
export type ApiContradictionDissent = Schemas['ContradictionDissent'];
export type ApiCertaintyCreate = Schemas['CertaintyCreate'];
export type ApiCertaintyRatings = Schemas['CertaintyRatings'];
export type ApiCertainty = Schemas['CertaintyResponse'];
export type ApiCertaintyLevel = NonNullable<ApiCertainty['level']>;
