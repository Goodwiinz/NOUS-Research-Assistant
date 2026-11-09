// SidebarFooter only renders inside its Sidebar parent - the full parent
// composition is the true render (composed inline; see learnings D.md on why
// sibling-preview imports are avoided).
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupAction,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInput,
  SidebarInset,
  SidebarMenu,
  SidebarMenuAction,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSkeleton,
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
  SidebarProvider,
  SidebarRail,
  SidebarSeparator,
  SidebarTrigger,
} from '@nous/design-system';
import {
  Files,
  FolderKanban,
  LayoutDashboard,
  MessageSquare,
  MoreHorizontal,
  Network,
  Plus,
  Settings,
} from 'lucide-react';

// The whole sidebar family in one research-workspace shell. SidebarProvider
// must wrap both <Sidebar> and <SidebarInset>; the desktop sidebar is
// position:fixed to the viewport (h-svh), so it needs a full-height frame.
const nav = [
  { title: 'Overview', icon: LayoutDashboard },
  { title: 'Chat', icon: MessageSquare, badge: '3' },
  { title: 'Documents', icon: Files, badge: '38' },
  { title: 'Entities', icon: Network },
];

export const ResearchWorkspace = () => (
  <SidebarProvider>
    <Sidebar collapsible="icon">
      <SidebarHeader className="border-b border-sidebar-border">
        <div className="px-2 py-1 font-mono text-xs font-semibold tracking-wider text-primary">
          NOUS
        </div>
        <SidebarInput placeholder="Search projects…" />
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>Workspace</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {nav.map(({ title, icon: Icon, badge }) => (
                <SidebarMenuItem key={title}>
                  <SidebarMenuButton isActive={title === 'Chat'} tooltip={title}>
                    <Icon />
                    <span>{title}</span>
                  </SidebarMenuButton>
                  {badge && <SidebarMenuBadge>{badge}</SidebarMenuBadge>}
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
        <SidebarSeparator />
        <SidebarGroup>
          <SidebarGroupLabel>Projects</SidebarGroupLabel>
          <SidebarGroupAction title="New project">
            <Plus />
            <span className="sr-only">New project</span>
          </SidebarGroupAction>
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton>
                  <FolderKanban />
                  <span>Sparse attention survey</span>
                </SidebarMenuButton>
                <SidebarMenuAction showOnHover={false}>
                  <MoreHorizontal />
                  <span className="sr-only">Project options</span>
                </SidebarMenuAction>
                <SidebarMenuSub>
                  <SidebarMenuSubItem>
                    <SidebarMenuSubButton href="#" isActive>
                      <span>Related work draft</span>
                    </SidebarMenuSubButton>
                  </SidebarMenuSubItem>
                  <SidebarMenuSubItem>
                    <SidebarMenuSubButton href="#">
                      <span>Benchmark comparison</span>
                    </SidebarMenuSubButton>
                  </SidebarMenuSubItem>
                </SidebarMenuSub>
              </SidebarMenuItem>
              <SidebarMenuItem>
                <SidebarMenuButton>
                  <FolderKanban />
                  <span>RAG evaluation</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
        <SidebarGroup>
          <SidebarGroupLabel>Recent threads</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuSkeleton showIcon />
              </SidebarMenuItem>
              <SidebarMenuItem>
                <SidebarMenuSkeleton showIcon />
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
      <SidebarFooter className="border-t border-sidebar-border">
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton size="lg">
              <div className="flex h-8 w-8 items-center justify-center rounded-md bg-primary/15 font-mono text-xs font-semibold text-primary">
                MR
              </div>
              <div className="grid flex-1 text-left text-sm leading-tight">
                <span className="truncate font-medium">Maya Rao</span>
                <span className="truncate text-xs text-muted-foreground">Research · Pro</span>
              </div>
              <Settings className="ml-auto" />
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
    <SidebarInset>
      <header className="flex h-14 shrink-0 items-center gap-3 border-b border-border px-4">
        <SidebarTrigger />
        <Breadcrumb>
          <BreadcrumbList className="text-xs">
            <BreadcrumbItem>
              <BreadcrumbLink href="#">Projects</BreadcrumbLink>
            </BreadcrumbItem>
            <BreadcrumbSeparator>/</BreadcrumbSeparator>
            <BreadcrumbItem>
              <BreadcrumbPage className="text-primary">Sparse attention survey</BreadcrumbPage>
            </BreadcrumbItem>
          </BreadcrumbList>
        </Breadcrumb>
      </header>
      <div className="flex flex-1 flex-col gap-4 p-6">
        <h1 className="text-xl font-semibold tracking-tight">Sparse attention survey</h1>
        <p className="text-sm text-muted-foreground">
          14 sources · 3 threads · last agent run 12 minutes ago
        </p>
      </div>
    </SidebarInset>
  </SidebarProvider>
);
