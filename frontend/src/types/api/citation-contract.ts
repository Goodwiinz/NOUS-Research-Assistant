/**
 * Raw citation HTTP shapes generated from the FastAPI OpenAPI contract.
 * Presentation-facing camelCase types live in `types/research.ts`; the
 * citation service is the adapter between these wire shapes and that model.
 */
import type { components } from '@/types/generated/api';

export type ApiCitationCreate =
  components['schemas']['src__shared__research_schemas__CitationCreate'];
export type ApiCitationResponse =
  components['schemas']['src__shared__research_schemas__CitationResponse'];
export type ApiCitationListResponse =
  components['schemas']['CitationListResponse'];
export type ApiCitationExtractRequest =
  components['schemas']['CitationExtractRequest'];

// openapi-typescript marks Pydantic-defaulted fields as required. Callers may
// omit them and let the backend apply its declared defaults.
export type ApiCitationCreateInput = Omit<
  ApiCitationCreate,
  'document_type' | 'metadata_source' | 'needs_review'
> &
  Partial<
    Pick<
      ApiCitationCreate,
      'document_type' | 'metadata_source' | 'needs_review'
    >
  >;
