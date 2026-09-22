import { Metadata } from 'next';

export const metadata: Metadata = {
  title: {
    template: '%s | NOUS',
    default: 'Sign In | NOUS',
  },
};

export default function AuthLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <div className="min-h-screen bg-(--nous-selene)">{children}</div>;
}
