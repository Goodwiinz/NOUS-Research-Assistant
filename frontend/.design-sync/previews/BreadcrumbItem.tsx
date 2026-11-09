// BreadcrumbItem only renders inside its Breadcrumb parent - the full parent
// composition is the true render (composed inline; see learnings D.md on why
// sibling-preview imports are avoided).
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

