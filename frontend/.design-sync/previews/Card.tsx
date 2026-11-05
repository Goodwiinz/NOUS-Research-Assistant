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
import { DarkSurface } from './_DarkSurface';
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

// Facts read as a short list, not a big-number metric tile.
export const ProjectFacts = () => (
  <Card style={{ width: 320 }}>
    <CardHeader className="pb-2">
      <CardTitle className="text-base">Citation check</CardTitle>
      <CardDescription>Sparse attention survey</CardDescription>
    </CardHeader>
    <CardContent>
      <dl className="space-y-2 text-sm">
        <div className="flex justify-between gap-4">
          <dt className="text-muted-foreground">Verified</dt>
          <dd className="font-medium">142 of 150</dd>
        </div>
        <div className="flex justify-between gap-4">
          <dt className="text-muted-foreground">Needs a source</dt>
          <dd className="font-medium">8</dd>
        </div>
        <div className="flex justify-between gap-4">
          <dt className="text-muted-foreground">Last checked</dt>
          <dd className="font-medium">Nov 3, 2026</dd>
        </div>
      </dl>
    </CardContent>
  </Card>
);

export const Interactive = () => (
  <Card interactive className="w-[380px] cursor-pointer">
    <CardHeader>
      <CardTitle className="text-base">Attention Is All You Need</CardTitle>
      <CardDescription>Vaswani et al. · NeurIPS 2017</CardDescription>
    </CardHeader>
    <CardContent>
      <p className="text-sm text-muted-foreground line-clamp-2">
        We propose the Transformer, a model architecture based solely on
        attention mechanisms, dispensing with recurrence and convolutions.
      </p>
    </CardContent>
  </Card>
);

// The same project card on the dark theme, the live app's default.
export const Dark = () => (
  <DarkSurface>
    <ProjectCard />
  </DarkSurface>
);
