// TooltipContent only renders inside its parent - the parent's composition is the true render.
import { ToolbarHint as ParentToolbarHint } from './Tooltip';

export const ToolbarHint = () => <ParentToolbarHint />;
