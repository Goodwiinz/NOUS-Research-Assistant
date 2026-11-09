import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSkeleton,
  SidebarProvider,
} from '@nous/design-system';
import { FolderOpen, MessageSquare } from 'lucide-react';

// Loading state of a sidebar group: skeleton rows hold the place of threads
// still being fetched, beside a group that has already loaded. Sidebar is
// collapsible="none" so it lays out in flow inside the card.
export const LoadingThreads = () => (
  <SidebarProvider style={{ minHeight: 0 }}>
    <Sidebar collapsible="none" className="h-[420px] border-r">
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>Projects</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton isActive>
                  <FolderOpen />
                  <span>Sparse attention survey</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
              <SidebarMenuItem>
                <SidebarMenuButton>
                  <FolderOpen />
                  <span>RAG evaluation</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
        <SidebarGroup>
          <SidebarGroupLabel>
            <MessageSquare className="mr-2" />
            Recent threads
          </SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {[0, 1, 2, 3].map((i) => (
                <SidebarMenuItem key={i}>
                  <SidebarMenuSkeleton showIcon />
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
    </Sidebar>
  </SidebarProvider>
);
