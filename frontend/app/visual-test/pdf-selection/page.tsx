import { notFound } from 'next/navigation';
import { PdfSelectionFixture } from './PdfSelectionFixture';

export default function PdfSelectionPage(): React.ReactElement {
  if (process.env.NEXT_PUBLIC_VISUAL_TEST_FIXTURES !== '1') notFound();
  return <PdfSelectionFixture />;
}
