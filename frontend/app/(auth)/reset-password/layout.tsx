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
  // Every state of this page is a dark nyx card inside the always-light auth
  // layout, so it opts back in to dark native controls and dark autofill.
  return (
    <div className="nous-dark-surface" style={{ colorScheme: 'dark' }}>
      {children}
    </div>
  );
}
