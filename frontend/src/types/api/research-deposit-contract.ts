/**
 * Type aliases for the GOO-318 archive deposit HTTP boundary (Zenodo
 * sandbox), sourced directly from the generated OpenAPI contract
 * (`frontend/src/types/generated/api.d.ts`, regenerated via
 * `pnpm --dir frontend generate:api-types`). Never re-type these shapes.
 */
import type { components } from '@/types/generated/api';

type Schemas = components['schemas'];

export type ApiDepositList = Schemas['DepositListResponse'];
export type ApiDeposit = Schemas['DepositResponse'];
export type ApiDepositStatus = ApiDeposit['status'];
export type ApiDepositAttempt = Schemas['DepositAttemptResponse'];
export type ApiDepositApproval = Schemas['DepositApprovalResponse'];
export type ApiDepositApprovalCreate = Schemas['DepositApprovalCreate'];
export type ApiDepositApprovalRevoke = Schemas['DepositApprovalRevoke'];
export type ApiDepositCreate = Schemas['DepositCreate'];
