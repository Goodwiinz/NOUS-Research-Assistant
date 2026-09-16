import { notFound } from 'next/navigation';

import { ChatMobileControlsFixture } from './ChatMobileControlsFixture';

export default function ChatMobileControlsVisualPage(): React.ReactElement {
  if (process.env.NEXT_PUBLIC_VISUAL_TEST_FIXTURES !== '1') notFound();

  return <ChatMobileControlsFixture />;
}
