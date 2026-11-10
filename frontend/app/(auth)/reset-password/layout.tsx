import { Metadata } from 'next';

// The page is a client component, so its tab title lives here. The auth
// layout's template appends " | NOUS".
export const metadata: Metadata = {
  title: 'Choose a new password',
};

export default function Layout({
  children,
}: {
  children: React.ReactNode;
}): React.ReactNode {
  return children;
}
