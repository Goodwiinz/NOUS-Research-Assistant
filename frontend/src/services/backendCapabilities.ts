/** Frontend-owned compatibility discovery, independent of user/project data. */
export interface BackendCapabilities {
  draftClaims: boolean;
  draftRelease: boolean;
}

export async function fetchBackendCapabilities(): Promise<BackendCapabilities> {
  const response = await fetch('/api/backend-capabilities', {
    cache: 'no-store',
  });
  if (!response.ok) throw new Error('Capabilities unavailable');
  const data: unknown = await response.json();
  if (
    !data ||
    typeof data !== 'object' ||
    !('draftClaims' in data) ||
    !('draftRelease' in data)
  )
    throw new Error('Capabilities unavailable');
  return {
    draftClaims: data.draftClaims === true,
    draftRelease: data.draftRelease === true,
  };
}
