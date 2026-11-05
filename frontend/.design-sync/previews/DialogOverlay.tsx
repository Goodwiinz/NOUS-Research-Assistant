// DialogOverlay only renders inside a modal Dialog - the parent's composition is the true render.
import { ExportCitations as DialogExportCitations } from './DialogTrigger';

export const ExportCitations = () => <DialogExportCitations />;
