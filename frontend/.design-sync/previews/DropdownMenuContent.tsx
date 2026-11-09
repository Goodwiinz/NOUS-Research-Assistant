// DropdownMenuContent only renders inside its parent - the parent's composition is the true render.
import { ThreadActions as ParentThreadActions } from './DropdownMenu';

export const ThreadActions = () => <ParentThreadActions />;
