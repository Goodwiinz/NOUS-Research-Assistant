import { Alert, AlertDescription, AlertTitle } from '@nous/design-system';
import { DarkSurface } from './_DarkSurface';
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

export const Destructive = () => (
  <Alert variant="destructive" className="max-w-xl">
    <AlertTriangle className="h-4 w-4" />
    <AlertTitle>Ingestion failed</AlertTitle>
    <AlertDescription>
      “attention-is-all-you-need.pdf” could not be parsed. The file may be
      scanned or password-protected. Upload a text-based PDF to retry.
    </AlertDescription>
  </Alert>
);

export const DescriptionOnly = () => (
  <Alert variant="destructive" className="max-w-xl">
    <AlertDescription className="space-y-2">
      <p className="font-medium">This device can change your library without asking.</p>
      <p>
        It can make reversible changes in every project in your workspace. Only
        approve devices you control.
      </p>
    </AlertDescription>
  </Alert>
);

// The same error alert on the dark theme, the live app's default.
export const Dark = () => (
  <DarkSurface>
    <Destructive />
  </DarkSurface>
);
