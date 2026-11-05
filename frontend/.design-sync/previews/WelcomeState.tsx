import { WelcomeState } from '@nous/design-system';

// WelcomeState fills an empty chat transcript. Each starter row drops its
// template into the composer via `onPromptSelect`.

export const EmptyThread = () => (
  <div className="h-[560px]">
    <WelcomeState onPromptSelect={() => {}} />
  </div>
);
