// TabsTrigger only renders inside Tabs - the parent composition is the true render.
// (Composed inline: sibling preview imports resolve to the DS shim, not the file.)
import {
  Badge,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '@nous/design-system';

export const ProjectSections = () => (
  <Tabs defaultValue="library" className="w-[420px]">
    <TabsList>
      <TabsTrigger value="library">Library</TabsTrigger>
      <TabsTrigger value="threads">Threads</TabsTrigger>
      <TabsTrigger value="drafts">Drafts</TabsTrigger>
      <TabsTrigger value="notes">Notes</TabsTrigger>
    </TabsList>
    <TabsContent value="library" className="space-y-2">
      <p className="text-sm font-medium">24 papers in this project</p>
      <p className="text-sm text-muted-foreground">
        Upload PDFs or import from arXiv to ground the agent’s answers in your sources.
      </p>
    </TabsContent>
    <TabsContent value="threads">Research threads</TabsContent>
    <TabsContent value="drafts">Drafts</TabsContent>
    <TabsContent value="notes">Notes</TabsContent>
  </Tabs>
);
