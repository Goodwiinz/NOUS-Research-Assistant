import {
  Breadcrumb,
  BreadcrumbEllipsis,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from '@nous/design-system';
import { Fragment } from 'react';

// Ported from SidebarLayout's page header: text-xs list, "/" separators,
// muted links and the current page in the primary (sol) tone.
export const PageHeader = () => (
  <header style={{ width: 640 }} className="flex h-14 items-center border-b border-border bg-background px-4">
    <Breadcrumb>
      <BreadcrumbList className="text-xs">
        {[
          ['/dashboard', 'Dashboard'],
          ['/projects', 'Projects'],
        ].map(([href, name]) => (
          <Fragment key={href}>
            <BreadcrumbItem>
              <BreadcrumbLink
                href={href}
                className="text-muted-foreground transition-colors hover:text-foreground"
              >
                {name}
              </BreadcrumbLink>
            </BreadcrumbItem>
            <BreadcrumbSeparator className="text-muted-foreground/50">/</BreadcrumbSeparator>
          </Fragment>
        ))}
        <BreadcrumbItem>
          <BreadcrumbPage className="text-primary">Sparse attention survey</BreadcrumbPage>
        </BreadcrumbItem>
      </BreadcrumbList>
    </Breadcrumb>
  </header>
);

// Default chevron separators and a collapsed middle segment.
export const CollapsedPath = () => (
  <Breadcrumb>
    <BreadcrumbList>
      <BreadcrumbItem>
        <BreadcrumbLink href="/dashboard">Dashboard</BreadcrumbLink>
      </BreadcrumbItem>
      <BreadcrumbSeparator />
      <BreadcrumbItem>
        <BreadcrumbEllipsis />
      </BreadcrumbItem>
      <BreadcrumbSeparator />
      <BreadcrumbItem>
        <BreadcrumbLink href="/documents">Documents</BreadcrumbLink>
      </BreadcrumbItem>
      <BreadcrumbSeparator />
      <BreadcrumbItem>
        <BreadcrumbPage>Attention Is All You Need.pdf</BreadcrumbPage>
      </BreadcrumbItem>
    </BreadcrumbList>
  </Breadcrumb>
);
