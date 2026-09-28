import { api } from '@/services/api-client';
import type { components } from '@/types/generated/api';

const BASE = '/api/v1/research-engine';

export type ResearchQuestionCreate =
  components['schemas']['ResearchQuestionCreate'];
export type ResearchQuestionResponse =
  components['schemas']['ResearchQuestionResponse'];
export type ResearchQuestionVersionCreate =
  components['schemas']['ResearchQuestionVersionCreate'];
export type ResearchProtocolCreate =
  components['schemas']['ResearchProtocolCreate'];
export type ProtocolSnapshot = components['schemas']['ProtocolSnapshot'];
export type ResearchProtocolResponse =
  components['schemas']['ResearchProtocolResponse'];
export type ResearchProtocolVersionCreate =
  components['schemas']['ResearchProtocolVersionCreate'];
export type ResearchProtocolVersionResponse =
  components['schemas']['ResearchProtocolVersionResponse'];
export type ProtocolApprovalRequest =
  components['schemas']['ProtocolApprovalRequest'];
export type ProtocolApprovalResponse =
  components['schemas']['ProtocolApprovalResponse'];
export type ProtocolRegistrationCreate =
  components['schemas']['ProtocolRegistrationCreate'];
export type ProtocolRegistrationResponse =
  components['schemas']['ProtocolRegistrationResponse'];
export type ProtocolDeviationCreate =
  components['schemas']['ProtocolDeviationCreate'];
export type ProtocolDeviationResponse =
  components['schemas']['ProtocolDeviationResponse'];

export const researchProtocolService = {
  listQuestions: (projectId: string): Promise<ResearchQuestionResponse[]> =>
    api.get(`${BASE}/projects/${projectId}/questions`),
  createQuestion: (
    projectId: string,
    body: ResearchQuestionCreate
  ): Promise<ResearchQuestionResponse> =>
    api.post(`${BASE}/projects/${projectId}/questions`, body),
  createQuestionVersion: (
    questionId: string,
    body: ResearchQuestionVersionCreate
  ): Promise<ResearchQuestionResponse> =>
    api.post(`${BASE}/questions/${questionId}/versions`, body),
  listProtocols: (projectId: string): Promise<ResearchProtocolResponse[]> =>
    api.get(`${BASE}/projects/${projectId}/protocols`),
  createProtocol: (
    projectId: string,
    body: ResearchProtocolCreate
  ): Promise<ResearchProtocolResponse> =>
    api.post(`${BASE}/projects/${projectId}/protocols`, body),
  getProtocol: (protocolId: string): Promise<ResearchProtocolResponse> =>
    api.get(`${BASE}/protocols/${protocolId}`),
  createProtocolVersion: (
    protocolId: string,
    body: ResearchProtocolVersionCreate
  ): Promise<ResearchProtocolResponse> =>
    api.post(`${BASE}/protocols/${protocolId}/versions`, body),
  approveProtocolVersion: (
    protocolId: string,
    versionId: string,
    body: ProtocolApprovalRequest
  ): Promise<ProtocolApprovalResponse> =>
    api.post(
      `${BASE}/protocols/${protocolId}/versions/${versionId}/approve`,
      body
    ),
  listRegistrations: (
    protocolId: string
  ): Promise<ProtocolRegistrationResponse[]> =>
    api.get(`${BASE}/protocols/${protocolId}/registrations`),
  recordRegistration: (
    protocolId: string,
    body: ProtocolRegistrationCreate
  ): Promise<ProtocolRegistrationResponse> =>
    api.post(`${BASE}/protocols/${protocolId}/registrations`, body),
  listDeviations: (protocolId: string): Promise<ProtocolDeviationResponse[]> =>
    api.get(`${BASE}/protocols/${protocolId}/deviations`),
  recordDeviation: (
    protocolId: string,
    body: ProtocolDeviationCreate
  ): Promise<ProtocolDeviationResponse> =>
    api.post(`${BASE}/protocols/${protocolId}/deviations`, body),
};
