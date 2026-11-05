// HoverCardTrigger only renders inside its parent - the parent's composition is the true render.
import { CitationPreview as ParentCitationPreview } from './HoverCard';

export const CitationPreview = () => <ParentCitationPreview />;
