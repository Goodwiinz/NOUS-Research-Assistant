import { useQuery } from '@tanstack/react-query';
import {
  fetchBackendCapabilities,
  type BackendCapabilities,
} from '@/services/backendCapabilities';

/** One shared Query owner; refresh after rollout or rollback, fail closed. */
export function useBackendCapabilities(enabled = true): BackendCapabilities {
  const query = useQuery({
    queryKey: ['backend-capabilities'],
    queryFn: fetchBackendCapabilities,
    enabled,
    staleTime: 30_000,
    refetchInterval: 30_000,
    retry: false,
  });
  const known = enabled && query.isSuccess;
  return {
    draftClaims: known && query.data.draftClaims,
    draftRelease: known && query.data.draftRelease,
  };
}
