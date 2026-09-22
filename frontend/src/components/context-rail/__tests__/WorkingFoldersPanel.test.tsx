import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { WorkingFoldersPanel } from '../WorkingFoldersPanel';
import { useProjectWorkingFolders } from '../hooks/useProjectWorkingFolders';

vi.mock('../hooks/useProjectWorkingFolders', () => ({
  useProjectWorkingFolders: vi.fn(),
}));

const mockedUseProjectWorkingFolders = vi.mocked(useProjectWorkingFolders);

describe('WorkingFoldersPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedUseProjectWorkingFolders.mockReturnValue({
      documents: undefined,
      notes: undefined,
      drafts: undefined,
      isLoading: false,
      errors: {},
      refetch: vi.fn(),
    });
  });

  it('renders empty state when no project is bound', () => {
    render(<WorkingFoldersPanel allCitations={[]} />);
    expect(
      screen.getByText(/no files yet. cited sources will appear here/i)
    ).toBeInTheDocument();
    expect(screen.queryByText('Sources')).not.toBeInTheDocument();
    expect(screen.queryByText('Notes')).not.toBeInTheDocument();
    expect(screen.queryByText('Drafts')).not.toBeInTheDocument();
  });

  it('renders all four folders when the project has documents, notes, and drafts', () => {
    mockedUseProjectWorkingFolders.mockReturnValue({
      documents: [{ id: 'd1', title: 'Paper A' }],
      notes: [{ id: 'n1', title: 'Outline', isPinned: true }],
      drafts: [{ id: 'dr1', title: 'v1 draft', version: 1 }],
      isLoading: false,
      errors: {},
      refetch: vi.fn(),
    });
    render(<WorkingFoldersPanel allCitations={[]} projectId="p1" />);
    expect(screen.getByText('This thread')).toBeInTheDocument();
    expect(screen.getByText('Sources')).toBeInTheDocument();
    expect(screen.getByText('Notes')).toBeInTheDocument();
    expect(screen.getByText('Drafts')).toBeInTheDocument();
    expect(screen.getByText('Paper A')).toBeInTheDocument();
  });

  it('hides Sources/Notes/Drafts when their lists are empty', () => {
    mockedUseProjectWorkingFolders.mockReturnValue({
      documents: [],
      notes: [],
      drafts: [],
      isLoading: false,
      errors: {},
      refetch: vi.fn(),
    });
    render(<WorkingFoldersPanel allCitations={[]} projectId="p1" />);
    expect(
      screen.getByText(/no files yet. cited sources will appear here/i)
    ).toBeInTheDocument();
    expect(screen.queryByText('Sources')).not.toBeInTheDocument();
    expect(screen.queryByText('Notes')).not.toBeInTheDocument();
    expect(screen.queryByText('Drafts')).not.toBeInTheDocument();
  });

  it('shows a loading status instead of the empty state while fetching', () => {
    mockedUseProjectWorkingFolders.mockReturnValue({
      documents: undefined,
      notes: undefined,
      drafts: undefined,
      isLoading: true,
      errors: {},
      refetch: vi.fn(),
    });
    render(<WorkingFoldersPanel allCitations={[]} projectId="p1" />);
    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(screen.queryByText(/no files yet/i)).not.toBeInTheDocument();
  });

  it('shows an error with retry when a project list fails to load', () => {
    const refetch = vi.fn();
    mockedUseProjectWorkingFolders.mockReturnValue({
      documents: [],
      notes: [],
      drafts: [],
      isLoading: false,
      errors: { documents: new Error('500') },
      refetch,
    });
    render(<WorkingFoldersPanel allCitations={[]} projectId="p1" />);
    expect(screen.getByRole('alert')).toHaveTextContent(/couldn't load/i);
    expect(screen.queryByText(/no files yet/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /retry/i }));
    expect(refetch).toHaveBeenCalled();
  });
});
