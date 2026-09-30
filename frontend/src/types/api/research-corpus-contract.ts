/**
 * Type aliases for the GOO-300 search import / citation chase / corpus export
 * HTTP boundary, sourced directly from the generated OpenAPI contract
 * (`frontend/src/types/generated/api.d.ts`, regenerated via
 * `pnpm --dir frontend generate:api-types`). Never re-type these shapes.
 */
import type { components } from '@/types/generated/api';

export type ApiImportDeclaration = components['schemas']['ImportDeclaration'];
export type ApiCitationChaseDeclaration =
  components['schemas']['CitationChaseDeclaration'];
export type ApiImportReceipt = components['schemas']['ImportReceiptResponse'];
export type ApiImportReceiptDetail =
  components['schemas']['ImportReceiptDetail'];
export type ApiImportRecord = components['schemas']['ImportRecordResponse'];
export type ApiCitationChaseRequest =
  components['schemas']['CitationChaseRequest'];
export type ApiCoverageRequest = components['schemas']['CoverageRequest'];
export type ApiCoverage = components['schemas']['CoverageResponse'];
/** The multipart import body's `format` field (a free string on the wire). */
export type ApiImportFormat = 'ris' | 'csv' | 'nbib';
