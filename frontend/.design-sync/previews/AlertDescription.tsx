// AlertDescription only renders inside Alert - the parent composition is the true render.
// (Composed inline: sibling preview imports resolve to the DS shim, not the file.)
import { Alert, AlertDescription, AlertTitle } from '@nous/design-system';
import { AlertTriangle, Info } from 'lucide-react';

export const Default = () => (
  <Alert className="max-w-xl">
    <Info className="h-4 w-4" />
    <AlertTitle>Re-indexing in progress</AlertTitle>
    <AlertDescription>
      12 of 48 documents in “Sparse attention survey” are being re-chunked.
      Search results may be incomplete for the next few minutes.
    </AlertDescription>
  </Alert>
);
