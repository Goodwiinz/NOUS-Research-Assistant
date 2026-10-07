/**
 * Wire shapes for the selected-context routes, aliased from the generated
 * OpenAPI types (regenerate with `pnpm --dir frontend generate:api-types`).
 */
import type { components } from '@/types/generated/api';

export type ApiContextOptions = components['schemas']['ContextOptions'];
export type ApiMemoryOption = components['schemas']['MemoryOption'];
export type ApiContextSelectionUpdate =
  components['schemas']['ContextSelectionUpdate'];
