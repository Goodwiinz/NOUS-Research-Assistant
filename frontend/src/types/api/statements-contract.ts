/**
 * Type aliases for the GOO-316 statement set, approval, venue-check and
 * ORCID HTTP boundary, sourced directly from the generated OpenAPI contract
 * (`frontend/src/types/generated/api.d.ts`, regenerated via
 * `pnpm --dir frontend generate:api-types`). Never re-type these shapes.
 */
import type { components } from '@/types/generated/api';

type Schemas = components['schemas'];

export type ApiStatementsList = Schemas['StatementsListResponse'];
export type ApiStatementSet = Schemas['StatementSetResponse'];
export type ApiStatementSetCreate = Schemas['StatementSetCreate'];
export type ApiStatementBody = Schemas['StatementBody'];
export type ApiAuthorIn = Schemas['AuthorIn'];
export type ApiAuthorIdentity = Schemas['AuthorIdentity'];
export type ApiOrcidStatus = ApiAuthorIdentity['orcid_status'];
export type ApiApprovalCreate = Schemas['ApprovalCreate'];
export type ApiApproval = Schemas['ApprovalResponse'];
export type ApiVenueItem = Schemas['VenueItem'];
export type ApiVenueCheck = Schemas['VenueCheckResponse'];
export type ApiVenueCheckList = Schemas['VenueCheckListResponse'];
export type ApiOrcidStart = Schemas['OrcidStartResponse'];
export type ApiOrcidAuthentication = Schemas['OrcidAuthenticationResponse'];

/** NISO CRediT `credit/1` labels for the server's slugs (display only). */
export const CREDIT_LABELS: Record<string, string> = {
  conceptualization: 'Conceptualization',
  'data-curation': 'Data curation',
  'formal-analysis': 'Formal analysis',
  'funding-acquisition': 'Funding acquisition',
  investigation: 'Investigation',
  methodology: 'Methodology',
  'project-administration': 'Project administration',
  resources: 'Resources',
  software: 'Software',
  supervision: 'Supervision',
  validation: 'Validation',
  visualization: 'Visualization',
  'writing-original-draft': 'Writing – original draft',
  'writing-review-editing': 'Writing – review & editing',
};
