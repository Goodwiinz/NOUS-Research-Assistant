import { beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '@/services/api-client';
import { researchProtocolService } from '@/services/researchProtocolService';

vi.mock('@/services/api-client', () => ({
  api: { get: vi.fn(), post: vi.fn() },
}));

describe('researchProtocolService', () => {
  beforeEach(() => vi.clearAllMocks());

  it('keeps protocol operations scoped to canonical collection paths', async () => {
    vi.mocked(api.get).mockResolvedValue([]);
    vi.mocked(api.post).mockResolvedValue({ id: 'result-1' });
    const snapshot = {
      eligibility: {},
      sources_search: {},
      selection: {},
      extraction: {},
      appraisal_synthesis: {},
      outcomes: {},
      reviewer_mode: {},
    };

    await researchProtocolService.listQuestions('collection-1');
    await researchProtocolService.createQuestion('collection-1', {
      question: 'Does the intervention improve recovery?',
      framework: {},
    });
    await researchProtocolService.listProtocols('collection-1');
    await researchProtocolService.createProtocol('collection-1', {
      name: 'Recovery protocol',
      question_version_id: 'question-version-1',
      blueprint_id: 'blueprint-1',
      snapshot,
    });
    await researchProtocolService.createProtocolVersion('protocol-1', {
      question_version_id: 'question-version-1',
      blueprint_id: 'blueprint-1',
      snapshot,
      parent_version_id: 'protocol-version-1',
      amendment_reason: 'Clarify eligibility criteria',
    });

    expect(api.get).toHaveBeenNthCalledWith(
      1,
      '/api/v1/research-engine/projects/collection-1/questions'
    );
    expect(api.get).toHaveBeenNthCalledWith(
      2,
      '/api/v1/research-engine/projects/collection-1/protocols'
    );
    expect(api.post).toHaveBeenCalledWith(
      '/api/v1/research-engine/projects/collection-1/protocols',
      expect.objectContaining({ question_version_id: 'question-version-1' })
    );
    expect(api.post).toHaveBeenCalledWith(
      '/api/v1/research-engine/protocols/protocol-1/versions',
      expect.objectContaining({
        parent_version_id: 'protocol-version-1',
        amendment_reason: 'Clarify eligibility criteria',
      })
    );
  });

  it('binds approval to the exact version, hash, and approved pointer', async () => {
    vi.mocked(api.post).mockResolvedValue({ id: 'protocol-1' });
    const approval = {
      expected_protocol_version: 2,
      expected_content_hash: 'a'.repeat(64),
      expected_current_approved_version_id: 'version-1',
      reason: 'Independent methodological review completed',
      idempotency_key: 'approval-key',
    };

    await researchProtocolService.approveProtocolVersion(
      'protocol-1',
      'version-2',
      approval
    );

    expect(api.post).toHaveBeenCalledWith(
      '/api/v1/research-engine/protocols/protocol-1/versions/version-2/approve',
      approval
    );
  });
});
