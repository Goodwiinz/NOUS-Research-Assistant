import { Breadcrumb, BreadcrumbItem, BreadcrumbList, BreadcrumbPage, ThemeToggle } from '@nous/design-system';

// Ported from SidebarLayout's page header: the toggle sits at the right end.
export const InPageHeader = () => (
  <header className="flex h-14 w-[420px] items-center gap-3 border-b border-border bg-background px-4">
    <Breadcrumb>
      <BreadcrumbList className="text-xs">
        <BreadcrumbItem>
          <BreadcrumbPage className="text-primary">Dashboard</BreadcrumbPage>
        </BreadcrumbItem>
      </BreadcrumbList>
    </Breadcrumb>
    <div className="ml-auto flex items-center gap-2">
      <ThemeToggle />
    </div>
  </header>
);

export const Standalone = () => <ThemeToggle />;
