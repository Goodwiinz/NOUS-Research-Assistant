/**
 * Type aliases for the GOO-314 peer-review and anchored-diff HTTP boundary,
 * sourced directly from the generated OpenAPI contract
 * (`frontend/src/types/generated/api.d.ts`, regenerated via
 * `pnpm --dir frontend generate:api-types`). Never re-type these shapes.
 */
import type { components } from '@/types/generated/api';

type Schemas = components['schemas'];

export type ApiPeerReviewRound = Schemas['RoundResponse'];
export type ApiPeerReviewRoundList = Schemas['RoundListResponse'];
export type ApiPeerReviewRoundDetail = Schemas['RoundDetail'];
export type ApiPeerReviewComment = Schemas['CommentDetail'];
export type ApiPeerReviewResponseCreate = Schemas['ResponseCreate'];
export type ApiPeerReviewResponse = Schemas['ResponseVersionResponse'];
export type ApiPeerReviewResponseView = Schemas['ResponseView'];
export type ApiPeerReviewDecisionCreate = Schemas['DecisionCreate'];
export type ApiPeerReviewDecision = Schemas['DecisionResponse'];
export type ApiAnchorState = ApiPeerReviewComment['anchor_state'];
export type ApiCommentStatus = ApiPeerReviewComment['status'];
export type ApiDiffHunk = Schemas['DiffHunk'];
export type ApiDraftDiff = Schemas['DraftDiffResponse'];
