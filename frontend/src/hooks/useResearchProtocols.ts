'use client';

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';

import {
  researchProtocolService,
  type ProtocolApprovalRequest,
  type ProtocolApprovalResponse,
  type ProtocolDeviationCreate,
  type ProtocolRegistrationCreate,
  type ProtocolRegistrationResponse,
  type ProtocolDeviationResponse,
  type ResearchProtocolCreate,
  type ResearchProtocolResponse,
  type ResearchProtocolVersionCreate,
  type ResearchQuestionCreate,
  type ResearchQuestionResponse,
  type ResearchQuestionVersionCreate,
} from '@/services/researchProtocolService';

export const researchProtocolKeys = {
  root: (projectId: string) => ['project', projectId, 'protocols'] as const,
  questions: (projectId: string) =>
    [...researchProtocolKeys.root(projectId), 'questions'] as const,
  protocols: (projectId: string) =>
    [...researchProtocolKeys.root(projectId), 'list'] as const,
  registrations: (projectId: string, protocolId: string) =>
    [
      ...researchProtocolKeys.root(projectId),
      protocolId,
      'registrations',
    ] as const,
  deviations: (projectId: string, protocolId: string) =>
    [
      ...researchProtocolKeys.root(projectId),
      protocolId,
      'deviations',
    ] as const,
};

export function useResearchQuestions(
  projectId: string
): UseQueryResult<ResearchQuestionResponse[], Error> {
  return useQuery({
    queryKey: researchProtocolKeys.questions(projectId),
    queryFn: () => researchProtocolService.listQuestions(projectId),
    enabled: Boolean(projectId),
  });
}

export function useResearchProtocols(
  projectId: string
): UseQueryResult<ResearchProtocolResponse[], Error> {
  return useQuery({
    queryKey: researchProtocolKeys.protocols(projectId),
    queryFn: () => researchProtocolService.listProtocols(projectId),
    enabled: Boolean(projectId),
  });
}

export interface ResearchProtocolActions {
  createQuestion: UseMutationResult<
    ResearchQuestionResponse,
    Error,
    ResearchQuestionCreate
  >;
  createQuestionVersion: UseMutationResult<
    ResearchQuestionResponse,
    Error,
    { questionId: string; body: ResearchQuestionVersionCreate }
  >;
  createProtocol: UseMutationResult<
    ResearchProtocolResponse,
    Error,
    ResearchProtocolCreate
  >;
  createVersion: UseMutationResult<
    ResearchProtocolResponse,
    Error,
    { protocolId: string; body: ResearchProtocolVersionCreate }
  >;
  approve: UseMutationResult<
    ProtocolApprovalResponse,
    Error,
    { protocolId: string; versionId: string; body: ProtocolApprovalRequest }
  >;
  register: UseMutationResult<
    ProtocolRegistrationResponse,
    Error,
    { protocolId: string; body: ProtocolRegistrationCreate }
  >;
  recordDeviation: UseMutationResult<
    ProtocolDeviationResponse,
    Error,
    { protocolId: string; body: ProtocolDeviationCreate }
  >;
}

function useProtocolMutation<TVariables, TResult>(
  projectId: string,
  mutationFn: (variables: TVariables) => Promise<TResult>
): UseMutationResult<TResult, Error, TVariables> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: researchProtocolKeys.root(projectId),
      });
    },
  });
}

export function useProtocolActions(projectId: string): ResearchProtocolActions {
  return {
    createQuestion: useProtocolMutation<
      ResearchQuestionCreate,
      ResearchQuestionResponse
    >(projectId, (body) =>
      researchProtocolService.createQuestion(projectId, body)
    ),
    createQuestionVersion: useProtocolMutation<
      { questionId: string; body: ResearchQuestionVersionCreate },
      ResearchQuestionResponse
    >(projectId, ({ questionId, body }) =>
      researchProtocolService.createQuestionVersion(questionId, body)
    ),
    createProtocol: useProtocolMutation<
      ResearchProtocolCreate,
      ResearchProtocolResponse
    >(projectId, (body) =>
      researchProtocolService.createProtocol(projectId, body)
    ),
    createVersion: useProtocolMutation<
      { protocolId: string; body: ResearchProtocolVersionCreate },
      ResearchProtocolResponse
    >(projectId, ({ protocolId, body }) =>
      researchProtocolService.createProtocolVersion(protocolId, body)
    ),
    approve: useProtocolMutation<
      { protocolId: string; versionId: string; body: ProtocolApprovalRequest },
      ProtocolApprovalResponse
    >(projectId, ({ protocolId, versionId, body }) =>
      researchProtocolService.approveProtocolVersion(
        protocolId,
        versionId,
        body
      )
    ),
    register: useProtocolMutation<
      { protocolId: string; body: ProtocolRegistrationCreate },
      ProtocolRegistrationResponse
    >(projectId, ({ protocolId, body }) =>
      researchProtocolService.recordRegistration(protocolId, body)
    ),
    recordDeviation: useProtocolMutation<
      { protocolId: string; body: ProtocolDeviationCreate },
      ProtocolDeviationResponse
    >(projectId, ({ protocolId, body }) =>
      researchProtocolService.recordDeviation(protocolId, body)
    ),
  };
}

export function useProtocolRegistrations(
  projectId: string,
  protocolId?: string
): UseQueryResult<ProtocolRegistrationResponse[], Error> {
  return useQuery({
    queryKey: researchProtocolKeys.registrations(projectId, protocolId ?? ''),
    queryFn: () => researchProtocolService.listRegistrations(protocolId!),
    enabled: Boolean(protocolId),
  });
}

export function useProtocolDeviations(
  projectId: string,
  protocolId?: string
): UseQueryResult<ProtocolDeviationResponse[], Error> {
  return useQuery({
    queryKey: researchProtocolKeys.deviations(projectId, protocolId ?? ''),
    queryFn: () => researchProtocolService.listDeviations(protocolId!),
    enabled: Boolean(protocolId),
  });
}
