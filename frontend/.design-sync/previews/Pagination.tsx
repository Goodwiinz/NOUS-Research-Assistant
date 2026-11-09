import { Pagination } from '@nous/design-system';

const noop = () => {};

export const DocumentLibrary = () => (
  <Pagination currentPage={3} totalPages={12} pageSize={20} totalItems={231} onPageChange={noop} />
);

export const FirstPage = () => (
  <Pagination currentPage={1} totalPages={8} pageSize={25} totalItems={187} onPageChange={noop} />
);

export const LastPage = () => (
  <Pagination currentPage={12} totalPages={12} pageSize={20} totalItems={231} onPageChange={noop} />
);

export const FewPages = () => (
  <Pagination currentPage={2} totalPages={4} pageSize={10} totalItems={36} onPageChange={noop} />
);
