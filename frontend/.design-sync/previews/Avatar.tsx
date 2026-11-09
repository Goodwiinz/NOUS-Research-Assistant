import { Avatar, AvatarFallback, AvatarImage } from '@nous/design-system';

export const Initials = () => (
  <div className="flex items-center gap-3">
    <Avatar>
      <AvatarFallback className="bg-primary/10 text-primary font-medium">AV</AvatarFallback>
    </Avatar>
    <div className="leading-tight">
      <p className="text-sm font-medium">Ashish Vaswani</p>
      <p className="text-xs text-muted-foreground">Project owner</p>
    </div>
  </div>
);

export const Sizes = () => (
  <div className="flex items-end gap-4">
    <Avatar className="h-6 w-6">
      <AvatarFallback className="text-[10px] font-medium">NS</AvatarFallback>
    </Avatar>
    <Avatar className="h-8 w-8">
      <AvatarFallback className="text-xs font-medium">NS</AvatarFallback>
    </Avatar>
    <Avatar>
      <AvatarFallback className="text-sm font-medium">NS</AvatarFallback>
    </Avatar>
    <Avatar className="h-14 w-14">
      <AvatarFallback className="bg-primary/10 text-primary text-lg font-medium">NS</AvatarFallback>
    </Avatar>
  </div>
);

export const Collaborators = () => (
  <div className="flex">
    {['JD', 'MK', 'LP', 'RS'].map((initials, i) => (
      <Avatar
        key={initials}
        className="h-8 w-8 border-2 border-background"
        style={i > 0 ? { marginLeft: -8 } : undefined}
      >
        <AvatarFallback className="text-xs font-medium">{initials}</AvatarFallback>
      </Avatar>
    ))}
    <Avatar className="h-8 w-8 border-2 border-background" style={{ marginLeft: -8 }}>
      <AvatarFallback className="bg-primary/10 text-primary text-xs font-medium">+3</AvatarFallback>
    </Avatar>
  </div>
);

export const ImageWithFallback = () => (
  <Avatar>
    <AvatarImage src="/missing-avatar.png" alt="Noam Shazeer" />
    <AvatarFallback className="bg-primary/10 text-primary font-medium">NS</AvatarFallback>
  </Avatar>
);
