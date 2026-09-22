import { notFound } from 'next/navigation';
import { Suspense } from 'react';

import { AgentCitationsVisualFixture } from './AgentCitationsVisualFixture';

export default function AgentCitationsVisualPage(): React.ReactElement {
  if (process.env.NEXT_PUBLIC_VISUAL_TEST_FIXTURES !== '1') notFound();

  return (
    <Suspense fallback={<main>Loading fixture…</main>}>
      <AgentCitationsVisualFixture />
    </Suspense>
  );
}
