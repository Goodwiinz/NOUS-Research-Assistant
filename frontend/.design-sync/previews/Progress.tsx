import { Progress } from '@nous/design-system';

export const Default = () => (
  <div className="w-[360px]">
    <Progress value={62} />
  </div>
);

export const IndexingStatus = () => (
  <div className="grid w-[360px] gap-2">
    <div className="flex items-center justify-between text-sm">
      <span className="font-medium">Indexing documents</span>
      <span className="text-muted-foreground">31 / 48</span>
    </div>
    <Progress value={65} className="h-2" />
  </div>
);

export const Values = () => (
  <div className="grid w-[360px] gap-4">
    <Progress value={0} className="h-2" />
    <Progress value={25} className="h-2" />
    <Progress value={75} className="h-2" />
    <Progress value={100} className="h-2" />
  </div>
);
