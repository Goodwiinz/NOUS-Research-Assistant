import { api } from '@/services/api-client';
import type {
  ApiActionDecision,
  ApiActionReview,
  ApiActionStatus,
} from '@/types/api/integration-action-contract';

const path = (invocationId: string): string =>
  `/integrations/actions/${encodeURIComponent(invocationId)}`;

/**
 * Browser-only reads and decisions for actions a connected harness asked
 * for. The backend refuses both calls from a CLI token.
 */
export const integrationActionService = {
  review(invocationId: string): Promise<ApiActionReview> {
    return api.get<ApiActionReview>(`${path(invocationId)}/review`);
  },
  decide(invocationId: string, approved: boolean): Promise<ApiActionStatus> {
    const body: ApiActionDecision = { approved };
    return api.post<ApiActionStatus>(`${path(invocationId)}/decision`, body);
  },
};
