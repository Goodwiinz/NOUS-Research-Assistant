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

export const FullWidth = () => (
  <Tabs defaultValue="sources" className="w-[420px]">
    <TabsList className="grid w-full grid-cols-3">
      <TabsTrigger value="answer">Answer</TabsTrigger>
      <TabsTrigger value="sources">Sources</TabsTrigger>
      <TabsTrigger value="steps">Steps</TabsTrigger>
    </TabsList>
    <TabsContent value="answer">Answer</TabsContent>
    <TabsContent value="sources" className="flex flex-wrap gap-2">
      <Badge variant="outline">Vaswani et al. 2017</Badge>
      <Badge variant="outline">Beltagy et al. 2020</Badge>
      <Badge variant="outline">Zaheer et al. 2020</Badge>
    </TabsContent>
    <TabsContent value="steps">Steps</TabsContent>
  </Tabs>
);

export const WithDisabled = () => (
  <Tabs defaultValue="overview" className="w-[420px]">
    <TabsList>
      <TabsTrigger value="overview">Overview</TabsTrigger>
      <TabsTrigger value="graph">Knowledge graph</TabsTrigger>
      <TabsTrigger value="exports" disabled>
        Exports
      </TabsTrigger>
    </TabsList>
    <TabsContent value="overview" className="text-sm text-muted-foreground">
      Last indexed 2 hours ago · 1,284 passages · 312 entities
    </TabsContent>
    <TabsContent value="graph">Knowledge graph</TabsContent>
  </Tabs>
);
