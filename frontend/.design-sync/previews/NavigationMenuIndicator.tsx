// NavigationMenuIndicator only renders inside its NavigationMenu parent - the full parent
// composition is the true render (composed inline; see learnings D.md on why
// sibling-preview imports are avoided).
import {
  NavigationMenu,
  NavigationMenuContent,
  NavigationMenuIndicator,
  NavigationMenuItem,
  NavigationMenuLink,
  NavigationMenuList,
  NavigationMenuTrigger,
} from '@nous/design-system';
import { BookOpen, FileText, Network, Search } from 'lucide-react';

// Ported from the marketing navbar (shadcn-io/navbar-02): a trigger with a
// mega-menu panel plus plain links. Rendered open via defaultValue.
const features = [
  { title: 'Cited answers', href: '/chat', icon: Search, body: 'Ask questions and get answers linked to the exact source passage.' },
  { title: 'Knowledge graph', href: '/entities', icon: Network, body: 'Explore entities and relations extracted from your documents.' },
  { title: 'Draft generator', href: '/research', icon: FileText, body: 'Turn a project into a literature review with inline citations.' },
];

export const ProductNav = () => (
  <NavigationMenu defaultValue="features">
    <NavigationMenuList className="gap-1">
      <NavigationMenuItem value="features">
        <NavigationMenuTrigger className="h-9 bg-transparent px-4 py-2 text-sm font-medium">
          Features
        </NavigationMenuTrigger>
        <NavigationMenuContent>
          {/* The app uses lg:grid-cols-[.75fr_1fr]; set inline here because the
              800px card viewport is below the lg breakpoint. */}
          <ul style={{ gridTemplateColumns: '.75fr 1fr' }} className="grid gap-3 p-4 md:w-[500px]">
            <li className="row-span-3">
              <NavigationMenuLink asChild>
                <a
                  href="/search"
                  className="flex h-full w-full select-none flex-col justify-end rounded-md bg-linear-to-b from-muted/50 to-muted p-6 no-underline outline-hidden"
                >
                  <BookOpen className="mb-2 h-6 w-6 opacity-70" aria-hidden />
                  <div className="mb-2 text-lg font-medium">Research workspace</div>
                  <p className="text-sm leading-tight text-muted-foreground">
                    Projects, documents and agent threads in one place.
                  </p>
                </a>
              </NavigationMenuLink>
            </li>
            {features.map(({ title, href, icon: Icon, body }) => (
              <li key={title}>
                <NavigationMenuLink asChild>
                  <a
                    href={href}
                    className="block select-none space-y-1 rounded-md p-3 leading-none no-underline outline-hidden transition-colors hover:bg-accent hover:text-accent-foreground"
                  >
                    <div className="flex items-start space-x-4">
                      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
                        <Icon className="h-5 w-5" />
                      </div>
                      <div className="space-y-1">
                        <div className="text-sm font-medium leading-tight">{title}</div>
                        <p className="line-clamp-2 text-xs leading-snug text-muted-foreground/90">{body}</p>
                      </div>
                    </div>
                  </a>
                </NavigationMenuLink>
              </li>
            ))}
          </ul>
        </NavigationMenuContent>
      </NavigationMenuItem>
      <NavigationMenuItem value="resources">
        <NavigationMenuTrigger className="h-9 bg-transparent px-4 py-2 text-sm font-medium">
          Resources
        </NavigationMenuTrigger>
        <NavigationMenuContent>
          <ul className="grid w-[400px] gap-3 p-4">
            {['Documentation', 'API reference', 'Changelog'].map((label) => (
              <li key={label}>
                <NavigationMenuLink href="#" className="block rounded-md p-3 text-sm font-medium hover:bg-accent">
                  {label}
                </NavigationMenuLink>
              </li>
            ))}
          </ul>
        </NavigationMenuContent>
      </NavigationMenuItem>
      <NavigationMenuItem>
        <NavigationMenuLink
          href="/pricing"
          className="inline-flex h-9 w-max items-center justify-center rounded-md bg-transparent px-4 py-2 text-sm font-medium transition-colors hover:bg-accent hover:text-accent-foreground"
        >
          Pricing
        </NavigationMenuLink>
      </NavigationMenuItem>
      <NavigationMenuIndicator />
    </NavigationMenuList>
  </NavigationMenu>
);
