import { Skeleton } from '@nous/design-system';

export const DocumentRow = () => (
  <div className="flex w-[360px] items-center gap-3">
    <Skeleton className="h-10 w-10 rounded-lg" />
    <div className="flex-1 space-y-2">
      <Skeleton className="h-4 w-3/4" />
      <Skeleton className="h-3 w-1/2" />
    </div>
  </div>
);

export const CardPlaceholder = () => (
  <div className="w-[360px] space-y-3 rounded-xl border p-6">
    <Skeleton className="h-5 w-2/3" />
    <Skeleton className="h-4 w-full" />
    <Skeleton className="h-4 w-4/5" />
    <div className="flex gap-2 pt-2">
      <Skeleton className="h-6 w-16 rounded-full" />
      <Skeleton className="h-6 w-20 rounded-full" />
    </div>
  </div>
);

export const List = () => (
  <div className="w-[360px] space-y-3">
    {[0, 1, 2].map((i) => (
      <div key={i} className="flex items-center gap-3">
        <Skeleton className="h-8 w-8 rounded-full" />
        <Skeleton className="h-4 flex-1" />
      </div>
    ))}
  </div>
);
