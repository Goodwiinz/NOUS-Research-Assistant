export function interleaveAgentQaCases<
  Baseline extends { id: string },
  FollowUp extends { id: string; afterCaseId: string },
>(
  baseline: Baseline[],
  followUps: FollowUp[]
): Array<
  | { source: 'baseline'; testCase: Baseline }
  | { source: 'follow-up'; testCase: FollowUp }
> {
  const baselineIds = new Set(baseline.map(({ id }) => id));
  const followUpsByAnchor = new Map<string, FollowUp[]>();
  for (const followUp of followUps) {
    if (!baselineIds.has(followUp.afterCaseId)) {
      throw new Error(
        `Follow-up ${followUp.id} has missing baseline anchor ${followUp.afterCaseId}`
      );
    }
    const group = followUpsByAnchor.get(followUp.afterCaseId) ?? [];
    group.push(followUp);
    followUpsByAnchor.set(followUp.afterCaseId, group);
  }

  const sequence: Array<
    | { source: 'baseline'; testCase: Baseline }
    | { source: 'follow-up'; testCase: FollowUp }
  > = [];
  for (const testCase of baseline) {
    sequence.push({ source: 'baseline', testCase });
    for (const followUp of followUpsByAnchor.get(testCase.id) ?? []) {
      sequence.push({ source: 'follow-up', testCase: followUp });
    }
  }
  return sequence;
}
