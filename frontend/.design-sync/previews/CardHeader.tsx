// CardHeader only renders inside Card - the parent composition is the true render.
// (Composed inline: sibling preview imports resolve to the DS shim, not the file.)
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@nous/design-system';
import { FileText } from 'lucide-react';

export const ProjectCard = () => (
  <Card className="w-[380px]">
    <CardHeader>
      <CardTitle className="text-lg">Sparse attention survey</CardTitle>
      <CardDescription>
        Efficient transformer variants for long-context retrieval.
      </CardDescription>
    </CardHeader>
    <CardContent className="space-y-3">
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <FileText className="h-4 w-4" />
        <span>24 papers · 3 drafts</span>
      </div>
      <div className="flex gap-2">
        <Badge variant="success">Indexed</Badge>
        <Badge variant="outline">Updated 2h ago</Badge>
      </div>
    </CardContent>
    <CardFooter className="justify-end gap-2">
      <Button variant="outline" size="sm">Share</Button>
      <Button size="sm">Open project</Button>
    </CardFooter>
  </Card>
);
