import { AppRail, NousProvider } from '@nous/design-system';

// AppRail is the 56px primary icon rail on the left of every signed-in page
// (SidebarLayout: `<div className="flex h-svh"><AppRail />…</div>`). It is
// prop-less: the active item comes from usePathname(), the avatar initials
// from useAuth(). It is `hidden md:flex`, so it only shows at >=768px.

export const Rail = () => (
  <div className="flex" style={{ height: 620 }}>
    <AppRail />
  </div>
);

export const BesideContent = () => (
  <div className="flex overflow-hidden rounded-lg border border-(--nous-border-1)" style={{ height: 620 }}>
    <AppRail />
    <main className="flex-1 bg-(--nous-bg-1) p-6">
      <h1 className="text-lg font-semibold text-(--nous-fg-1)" style={{ fontFamily: 'var(--nous-font-heading)' }}>
        Long-context attention survey
      </h1>
      <p className="mt-2 font-nous-body text-sm leading-relaxed text-(--nous-fg-2)" style={{ maxWidth: '48ch' }}>
        14 documents · 3 threads · last synthesis 2 hours ago
      </p>
    </main>
  </div>
);

// A nested NousProvider sets the route, so the Chat item shows as active.
export const ActiveRoute = () => (
  <NousProvider pathname="/chat">
    <div className="flex" style={{ height: 620 }}>
      <AppRail />
    </div>
  </NousProvider>
);
