// SheetDescription only renders inside its parent - the parent's composition is the true render.
import { ProjectSettings as ParentProjectSettings } from './Sheet';

export const ProjectSettings = () => <ParentProjectSettings />;
