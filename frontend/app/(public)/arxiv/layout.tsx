import { Metadata } from 'next';

// The root layout sets a plain title with no template, so the brand suffix
// is written out here (and in page.tsx, whose metadata wins over this).
export const metadata: Metadata = {
  title: 'arXiv | NOUS',
};

export default function Layout({ children }: { children: React.ReactNode }) {
  return children;
}
