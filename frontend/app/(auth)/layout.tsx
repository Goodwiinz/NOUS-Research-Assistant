import { Metadata } from 'next';

export const metadata: Metadata = {
  title: {
    template: '%s | NOUS',
    default: 'Sign in | NOUS',
  },
};

export default function AuthLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // nous-light: the auth pages are always-light brand surfaces, so every
  // theme-adaptive token inside resolves to its light value even when the app
  // theme is dark (see the .nous-light scope in app/globals.css).
  return (
    <div className="nous-light min-h-screen bg-(--nous-selene) text-foreground">
      {children}
    </div>
  );
}
