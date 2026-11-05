import { ErrorDisplay } from '@nous/design-system';

// Document processing errors: name the file and the step, then the fix.

export const ProcessingFailed = () => (
  <div style={{ maxWidth: 560 }}>
    <ErrorDisplay
      error="“longformer-2020.pdf” has no text layer, so NOUS could not extract passages. Upload a text-based PDF or run OCR first."
      errorType="processing"
      title="Couldn't read this PDF"
      onRetry={() => {}}
      onDismiss={() => {}}
    />
  </div>
);

export const Warning = () => (
  <div style={{ maxWidth: 560 }}>
    <ErrorDisplay
      error="Pages 14 to 16 are images. Their figures and tables won't appear in search results."
      severity="warning"
      title="Some pages were skipped"
    />
  </div>
);

export const Compact = () => (
  <div style={{ maxWidth: 560 }}>
    <ErrorDisplay
      error="Upload interrupted. Check your connection and try again."
      errorType="network"
      compact
      onRetry={() => {}}
    />
  </div>
);
