import { api } from '@/services/api-client';
import type {
  ApiContextOptions,
  ApiContextSelectionUpdate,
} from '@/types/api/integration-context-contract';

/** Browser-only: the backend refuses both calls from a CLI token. */
export const integrationContextService = {
  options(requestId: string): Promise<ApiContextOptions> {
    return api.get<ApiContextOptions>(
      `/integrations/context/options?request_id=${encodeURIComponent(requestId)}`
    );
  },
  save(requestId: string, memoryIds: string[]): Promise<ApiContextOptions> {
    const body: ApiContextSelectionUpdate = {
      request_id: requestId,
      memory_ids: memoryIds,
    };
    return api.put<ApiContextOptions>('/integrations/context/selection', body);
  },
};
