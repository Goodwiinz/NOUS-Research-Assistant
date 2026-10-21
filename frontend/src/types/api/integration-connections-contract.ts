/**
 * Wire shapes for the connected-devices routes, aliased from the generated
 * OpenAPI types (regenerate with `pnpm --dir frontend generate:api-types`).
 */
import type { components } from '@/types/generated/api';

export type ApiConnectedDevice = components['schemas']['ConnectedDevice'];
export type ApiConnectionConsent = components['schemas']['ConnectionConsent'];

/** What one approval reaches: one project, one workspace, or the whole
 * account. GrantRequestDTO.binding_kind carries the same values. */
export type ApiBindingKind = ApiConnectionConsent['kind'];

/** A project folder of a paired computer (`/devices/{id}/workspaces`):
 * `project_id` is always a string. */
export type ApiWorkspaceBinding = components['schemas']['WorkspaceBindingDTO'];
/** An account folder (`/devices/{id}/account-workspaces`): it belongs to no
 * project, so the wire shape has no `project_id` at all. */
export type ApiAccountWorkspaceBinding =
  components['schemas']['AccountWorkspaceBindingDTO'];
