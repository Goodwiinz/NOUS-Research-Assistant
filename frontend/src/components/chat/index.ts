export { ChatInput, default as ChatInputComponent } from './ChatInput';
export { CitationLink, default as CitationLinkComponent } from './CitationLink';
export {
  CitationPanel,
  default as CitationPanelComponent,
} from './CitationPanel';
export {
  CitationRenderer,
  default as CitationRendererComponent,
} from './CitationRenderer';
export { WelcomeState, default as WelcomeStateComponent } from './WelcomeState';

// Shared chat primitives
export { ChatBubble } from './shared/ChatBubble';
export { SearchComposer } from './shared/SearchComposer';
export {
  mapChatMessageToViewModel,
  mapSearchResultToChatMessages,
} from './shared/messageViewModel';

// Re-export types
export type { WelcomeStateProps } from './WelcomeState';
export type { ChatBubbleProps, ChatBubbleMessage } from './shared/ChatBubble';
export type { SearchComposerProps } from './shared/SearchComposer';
export type { ChatMessageViewModel } from './shared/messageViewModel';
export { ChatDialogs } from './ChatDialogs';
export { ChatMessageList } from './ChatMessageList';
