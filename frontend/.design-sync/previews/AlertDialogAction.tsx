// AlertDialogAction only renders inside its parent - the parent's composition is the true render.
import { ArchiveProject as ParentArchiveProject } from './AlertDialog';

export const ArchiveProject = () => <ParentArchiveProject />;
