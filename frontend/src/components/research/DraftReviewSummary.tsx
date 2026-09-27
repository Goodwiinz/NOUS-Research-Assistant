import type { ReactElement } from 'react';
import type { DraftReview } from '@/services/projectService';

export function DraftReviewSummary({
  review,
}: {
  review: DraftReview;
}): ReactElement {
  return (
    <div role="status">
      Citation review: <strong>{review.outcome}</strong>.{' '}
      {review.review.summary?.minor ?? 0} minor,{' '}
      {review.review.summary?.major ?? 0} unsupported,{' '}
      {review.review.summary?.unverified ?? 0} unknown,{' '}
      {review.review.uncited_assertions?.length ?? 0} uncited.
      <details>
        <summary>Review findings</summary>
        <ul>
          {review.review.verdicts?.map((finding, index) => (
            <li key={index}>
              support{' '}
              {finding.checks?.support?.status ?? finding.verdict ?? 'unknown'};
              identity {finding.checks?.identity?.status ?? 'unknown'};
              publication{' '}
              {finding.checks?.publication?.performed
                ? finding.checks.publication.status
                : 'unknown (not checked)'}
              . {finding.evidence || 'No evidence available'}
              {finding.checks?.publication?.observations?.map(
                (observation, observationIndex) => (
                  <span key={observationIndex}>
                    {' '}
                    Observed {observation.field} from {observation.source}.
                  </span>
                )
              )}
            </li>
          ))}
          {review.review.uncited_assertions?.map((finding, index) => (
            <li key={`uncited-${index}`}>uncited: {finding.text}</li>
          ))}
        </ul>
      </details>
      <details>
        <summary>Review limits</summary>
        <p>
          Conservative prose heuristic; factual classification is incomplete.
          Claim excerpt limit{' '}
          {String(review.review.coverage?.claim_excerpt_chars ?? 'unknown')}{' '}
          characters; source excerpt limit{' '}
          {String(review.review.coverage?.source_excerpt_chars ?? 'unknown')}{' '}
          characters.
        </p>
      </details>
      <details>
        <summary>Reviewed candidate</summary>
        <pre className="whitespace-pre-wrap">{review.candidate_content}</pre>
      </details>
    </div>
  );
}
