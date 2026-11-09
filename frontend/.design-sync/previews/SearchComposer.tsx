import { useState } from 'react';
import { SearchComposer } from '@nous/design-system';

// SearchComposer is the controlled bottom composer on /search. The send pill
// lights up sol gold once there is text; while a search runs it swaps to Stop.

export const WithQuery = () => {
  const [value, setValue] = useState(
    'Which papers in my library report wall-clock speedups for exact attention above 16k tokens?'
  );
  return (
    <SearchComposer
      value={value}
      onChange={setValue}
      onSubmit={() => {}}
      onStop={() => {}}
      isLoading={false}
    />
  );
};

export const Empty = () => {
  const [value, setValue] = useState('');
  return (
    <SearchComposer
      value={value}
      onChange={setValue}
      onSubmit={() => {}}
      onStop={() => {}}
      isLoading={false}
      placeholder="Search your corpus or ask a research question…"
    />
  );
};

export const Searching = () => (
  <SearchComposer
    value="Compare Longformer and BigBird on long-document QA benchmarks"
    onChange={() => {}}
    onSubmit={() => {}}
    onStop={() => {}}
    isLoading
  />
);

export const Disabled = () => (
  <SearchComposer
    value=""
    onChange={() => {}}
    onSubmit={() => {}}
    onStop={() => {}}
    isLoading={false}
    isDisabled
    placeholder="Select a project to search its documents"
  />
);
