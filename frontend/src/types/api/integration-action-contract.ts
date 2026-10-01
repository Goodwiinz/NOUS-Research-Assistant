/**
 * Wire shapes for the integration action routes, aliased from the generated
 * OpenAPI types (regenerate with `pnpm --dir frontend generate:api-types`).
 */
import type { components } from '@/types/generated/api';

export type ApiActionReview = components['schemas']['ActionReview'];
export type ApiActionStatus = components['schemas']['ActionStatus'];
export type ApiActionDecision = components['schemas']['ActionDecision'];
export type ActionState = ApiActionReview['state'];
