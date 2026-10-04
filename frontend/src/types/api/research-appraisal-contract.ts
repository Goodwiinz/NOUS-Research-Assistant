/**
 * Type aliases for the GOO-309 study-design appraisal HTTP boundary, sourced
 * directly from the generated OpenAPI contract
 * (`frontend/src/types/generated/api.d.ts`, regenerated via
 * `pnpm --dir frontend generate:api-types`). Never re-type these shapes.
 */
import type { components } from '@/types/generated/api';

type Schemas = components['schemas'];

export type ApiAppraisalList = Schemas['AppraisalListResponse'];
export type ApiAppraisalResult = Schemas['AppraisalResult'];
export type ApiAppraisal = Schemas['AppraisalResponse'];
export type ApiAppraisalSubmit = Schemas['AppraisalSubmit'];
export type ApiAppraisalAdjudicate = Schemas['AppraisalAdjudicate'];
export type ApiAppraisalDomain = Schemas['AppraisalDomain'];
export type ApiAppraisalInstrument = Schemas['AppraisalInstrument'];
export type ApiAppraisalEvidenceOption = Schemas['AppraisalEvidenceOption'];
export type ApiAppraisalStatus = ApiAppraisalResult['status'];
export type ApiAppraisalDesign = ApiAppraisalSubmit['study_design'];
