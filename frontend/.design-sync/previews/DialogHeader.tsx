// DialogHeader only renders inside its parent - the parent's composition is the true render.
import { RenameProject as ParentRenameProject } from './Dialog';

export const RenameProject = () => <ParentRenameProject />;
