import React, { useState, useCallback, useEffect, useMemo, useRef } from 'react';
import { debounce } from 'lodash-es';
import {
  DocumentPlusIcon,
  MagnifyingGlassIcon,
  AdjustmentsHorizontalIcon,
  TrashIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  ArrowDownIcon,
} from '@heroicons/react/24/outline';
import { useDocuments } from '@/hooks/useDocuments';
import { useAuthStore } from '@/stores/authStore';
import { DocumentCard } from './DocumentCard';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { IconButton } from '@/components/ui/icon-button';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Badge } from '@/components/ui/badge';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { Document, UI_CONFIG } from '@/types';
import toast from 'react-hot-toast';

// Helper function to format file sizes
const formatFileSize = (bytes: number): string => {
  if (bytes === 0) return '0 Bytes';
  const k = 1024;
  const sizes = ['Bytes', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
};

interface DocumentLibraryProps {
  className?: string;
  onDocumentSelect?: (document: Document) => void;
  onDocumentPreview?: (document: Document) => void;
}

export const DocumentLibrary: React.FC<DocumentLibraryProps> = ({
  className,
  onDocumentSelect,
  onDocumentPreview,
}) => {
  const bulkActionsMenuRef = useRef<HTMLDivElement>(null);

  const { isAuthenticated } = useAuthStore();
  const {
    documents,
    loading,
    error,
    pagination,
    filters,
    selectedDocuments,
    selectedCount,
    hasSelection,
    isAllSelected,
    fetchDocuments,
    updateFilters,
    updatePage,
    updatePageSize,
    selectDocument,
    selectAllDocuments,
    clearSelection,
    deleteDocument,
    deleteDocuments,
    refreshDocuments,
    retryDocument,
  } = useDocuments({ autoFetch: true });

  const [searchQuery, setSearchQuery] = useState(filters.search_term || '');
  const [selectedFileType, setSelectedFileType] = useState<string>('all');
  const [selectedStatus, setSelectedStatus] = useState<string>('all');
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [documentToDelete, setDocumentToDelete] = useState<Document | null>(
    null
  );
  const [showBatchDeleteDialog, setShowBatchDeleteDialog] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [deleteProgress, setDeleteProgress] = useState(0);
  const [showBulkActionsMenu, setShowBulkActionsMenu] = useState(false);
  const [retryingDocument, setRetryingDocument] = useState<string | null>(null);

  // Debounce the filter update (and its fetch) so typing doesn't fire one
  // request per keystroke (R4-L23); the input itself stays responsive since
  // setSearchQuery is not debounced.
  const debouncedUpdateFilters = useMemo(
    () =>
      debounce(
        (query: string) => updateFilters({ search_term: query || undefined }),
        UI_CONFIG.DEBOUNCE_DELAY_MS
      ),
    [updateFilters]
  );

  useEffect(() => () => debouncedUpdateFilters.cancel(), [debouncedUpdateFilters]);

  const handleSearch = useCallback(
    (query: string) => {
      setSearchQuery(query);
      debouncedUpdateFilters(query);
    },
    [debouncedUpdateFilters]
  );

  const handleFileTypeFilter = useCallback(
    (fileType: string) => {
      setSelectedFileType(fileType);
      if (fileType === 'all') {
        updateFilters({ file_types: undefined });
      } else {
        updateFilters({ file_types: [fileType as Document['file_type']] });
      }
    },
    [updateFilters]
  );

  const handleStatusFilter = useCallback(
    (status: string) => {
      setSelectedStatus(status);
      if (status === 'all') {
        updateFilters({ status: undefined });
      } else {
        updateFilters({ status: [status as Document['processing_status']] });
      }
    },
    [updateFilters]
  );

  const handleDocumentSelect = useCallback(
    (documentId: string) => {
      selectDocument(documentId);
    },
    [selectDocument]
  );

  const handleSelectAll = useCallback(() => {
    if (isAllSelected) {
      clearSelection();
    } else {
      selectAllDocuments();
    }
  }, [isAllSelected, selectAllDocuments, clearSelection]);

  const handleDocumentPreview = useCallback(
    (document: Document) => {
      onDocumentPreview?.(document);
    },
    [onDocumentPreview]
  );

  const handleDocumentDelete = useCallback((document: Document) => {
    setDocumentToDelete(document);
    setShowDeleteDialog(true);
  }, []);

  const handleConfirmDelete = useCallback(async () => {
    if (!documentToDelete) return;

    setIsDeleting(true);
    const title = documentToDelete.title;
    try {
      await deleteDocument(documentToDelete.id);
      setShowDeleteDialog(false);
      setDocumentToDelete(null);
      toast.success(`Deleted "${title}"`);
    } catch (error) {
      console.error('Failed to delete document:', error);
      const message = error instanceof Error ? error.message : 'Unknown error';
      toast.error(`Failed to delete "${title}": ${message}`);
    } finally {
      setIsDeleting(false);
    }
  }, [documentToDelete, deleteDocument]);

  const handleDeleteSelected = useCallback(async () => {
    setShowBatchDeleteDialog(true);
  }, []);

  const handleConfirmBatchDelete = useCallback(async () => {
    setIsDeleting(true);
    setDeleteProgress(0);

    const selectedIds = Array.from(selectedDocuments);
    const totalItems = selectedIds.length;

    try {
      // Single refetch after all deletes (R4-M22) — deleteDocuments
      // continues past per-id failures and reports them together.
      await deleteDocuments(selectedIds, (done, total) =>
        setDeleteProgress(Math.round((done / total) * 100))
      );

      toast.success(
        `Deleted ${totalItems} ${totalItems === 1 ? 'document' : 'documents'}`
      );
    } catch (error) {
      console.error('Failed to delete selected documents:', error);
      const message = error instanceof Error ? error.message : 'Unknown error';
      toast.error(`Failed to delete documents: ${message}`);
    } finally {
      // Always close — on partial failure the dialog would otherwise stay
      // open showing a stale "Delete 0 documents?" (selection is already
      // pruned by deleteDocuments by the time we get here).
      setShowBatchDeleteDialog(false);
      setIsDeleting(false);
      setDeleteProgress(0);
    }
  }, [selectedDocuments, deleteDocuments]);

  // Close bulk actions menu when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (
        bulkActionsMenuRef.current &&
        !bulkActionsMenuRef.current.contains(event.target as Node)
      ) {
        setShowBulkActionsMenu(false);
      }
    };

    document.addEventListener('mousedown', handleClickOutside);
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
    };
  }, []);

  const handlePageChange = useCallback(
    (page: number) => {
      updatePage(page);
    },
    [updatePage]
  );

  const handlePageSizeChange = useCallback(
    (pageSize: string) => {
      updatePageSize(parseInt(pageSize, 10));
    },
    [updatePageSize]
  );

  const handleRefresh = useCallback(() => {
    refreshDocuments();
  }, [refreshDocuments]);

  const handleDocumentRetry = useCallback(
    async (documentId: string) => {
      setRetryingDocument(documentId);
      try {
        await retryDocument(documentId);
        toast.success('Document retry initiated');
      } catch (error) {
        console.error('Failed to retry document:', error);
        const message =
          error instanceof Error ? error.message : 'Unknown error';
        toast.error(`Failed to retry document: ${message}`);
      } finally {
        setRetryingDocument(null);
      }
    },
    [retryDocument]
  );

  // File type options
  const fileTypeOptions = [
    { value: 'all', label: 'All Files' },
    { value: 'pdf', label: 'PDF' },
    { value: 'txt', label: 'Text' },
    { value: 'jpg', label: 'JPEG' },
    { value: 'png', label: 'PNG' },
    { value: 'mp3', label: 'MP3' },
    { value: 'mp4', label: 'MP4' },
  ];

  // Status options
  const statusOptions = [
    { value: 'all', label: 'All Status' },
    { value: 'queued', label: 'Queued' },
    { value: 'processing', label: 'Processing' },
    { value: 'indexed', label: 'Ready' },
    { value: 'failed', label: 'Failed' },
  ];

  // Page size options
  const pageSizeOptions = [
    { value: 10, label: '10 per page' },
    { value: 20, label: '20 per page' },
    { value: 50, label: '50 per page' },
    { value: 100, label: '100 per page' },
  ];

  return (
    <div className={cn('w-full space-y-6', className)}>
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-foreground">Documents</h1>
          <p className="text-sm text-muted-foreground">
            {pagination.total}{' '}
            {pagination.total === 1 ? 'document' : 'documents'}
          </p>
        </div>
        <Button
          onClick={handleRefresh}
          variant="outline"
          size="sm"
          disabled={loading}
        >
          Refresh
        </Button>
      </div>

      {/* Filters and Search */}
      <div className="flex flex-col space-y-4 sm:flex-row sm:space-y-0 sm:space-x-4">
        {/* Search */}
        <div className="relative flex-1">
          <MagnifyingGlassIcon className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            type="text"
            placeholder="Search documents..."
            value={searchQuery}
            onChange={(e: React.ChangeEvent<HTMLInputElement>) =>
              handleSearch(e.target.value)
            }
            className="pl-10"
          />
        </div>

        {/* File Type Filter */}
        <Select value={selectedFileType} onValueChange={handleFileTypeFilter}>
          <SelectTrigger className="w-40">
            <SelectValue placeholder="File type" />
          </SelectTrigger>
          <SelectContent>
            {fileTypeOptions.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        {/* Status Filter */}
        <Select value={selectedStatus} onValueChange={handleStatusFilter}>
          <SelectTrigger className="w-40">
            <SelectValue placeholder="Status" />
          </SelectTrigger>
          <SelectContent>
            {statusOptions.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {/* Bulk Actions */}
      {hasSelection && (
        <div className="flex items-center justify-between p-4 bg-accent/50 border rounded-lg">
          <div className="flex items-center space-x-2">
            <Badge variant="secondary">{selectedCount} selected</Badge>
            <Button variant="ghost" size="sm" onClick={clearSelection}>
              Clear selection
            </Button>
          </div>
          <div className="flex items-center space-x-2">
            {/* Bulk Actions Dropdown */}
            <div className="relative" ref={bulkActionsMenuRef}>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setShowBulkActionsMenu(!showBulkActionsMenu)}
                className="flex items-center space-x-2"
                aria-expanded={showBulkActionsMenu}
                aria-haspopup="menu"
                aria-label="Bulk actions menu"
                id="bulk-actions-button"
              >
                <span>Actions</span>
                <ArrowDownIcon className="h-4 w-4" />
              </Button>

              {showBulkActionsMenu && (
                <div
                  className="absolute right-0 mt-2 w-48 bg-white rounded-md shadow-lg border border-border z-10"
                  role="menu"
                  aria-labelledby="bulk-actions-button"
                >
                  <div className="py-1">
                    <button
                      role="menuitem"
                      onClick={() => {
                        handleDeleteSelected();
                        setShowBulkActionsMenu(false);
                      }}
                      className="flex items-center w-full px-4 py-2 text-sm text-red-600 hover:bg-red-50"
                    >
                      <TrashIcon className="h-4 w-4 mr-2" aria-hidden="true" />
                      Delete Selected
                    </button>
                  </div>
                </div>
              )}
            </div>

            {/* Quick Delete Button */}
            <Button
              variant="outline"
              size="sm"
              onClick={handleDeleteSelected}
              className="text-destructive hover:text-destructive"
            >
              <TrashIcon className="h-4 w-4 mr-2" />
              Delete
            </Button>
          </div>
        </div>
      )}

      {/* Documents Grid */}
      <div className="space-y-4">
        {loading && (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {Array.from({ length: 6 }).map((_, index) => (
              <div
                key={index}
                className="h-48 bg-muted rounded-lg animate-pulse"
              />
            ))}
          </div>
        )}

        {error && (
          <div role="alert" className="text-center py-12">
            <p className="text-destructive mb-4">{error}</p>
            <Button onClick={handleRefresh} variant="outline">
              Try Again
            </Button>
          </div>
        )}

        {!loading && !error && documents.length === 0 && (
          <div className="text-center py-12">
            <DocumentPlusIcon className="h-12 w-12 text-muted-foreground mx-auto mb-4" />
            <h3 className="text-lg font-medium text-foreground mb-2">
              {!isAuthenticated ? 'Please log in' : 'No documents found'}
            </h3>
            <p className="text-muted-foreground">
              {!isAuthenticated
                ? 'You need to log in to view and upload documents'
                : searchQuery ||
                    selectedFileType !== 'all' ||
                    selectedStatus !== 'all'
                  ? 'Try adjusting your filters or search terms'
                  : 'Upload your first document to get started'}
            </p>
            {!isAuthenticated && (
              <Button
                onClick={() => (window.location.href = '/login')}
                className="mt-4"
              >
                Go to Login
              </Button>
            )}
          </div>
        )}

        {!loading && !error && documents.length > 0 && (
          <>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {documents.map((document) => (
                <DocumentCard
                  key={document.id}
                  document={document}
                  selected={selectedDocuments.has(document.id)}
                  onSelect={handleDocumentSelect}
                  onPreview={handleDocumentPreview}
                  onDelete={(documentId: string) => {
                    const document = documents.find((d) => d.id === documentId);
                    if (document) {
                      handleDocumentDelete(document);
                    }
                  }}
                  onRetry={handleDocumentRetry}
                />
              ))}
            </div>

            {/* Pagination */}
            <div className="flex items-center justify-between">
              <div className="text-sm text-muted-foreground">
                Showing {(pagination.page - 1) * pagination.pageSize + 1} to{' '}
                {Math.min(
                  pagination.page * pagination.pageSize,
                  pagination.total
                )}{' '}
                of {pagination.total} documents
              </div>

              <div className="flex items-center space-x-4">
                <div className="flex items-center space-x-2">
                  <span className="text-sm text-muted-foreground">
                    Items per page:
                  </span>
                  <Select
                    value={pagination.pageSize.toString()}
                    onValueChange={handlePageSizeChange}
                  >
                    <SelectTrigger className="w-32">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {pageSizeOptions.map((option) => (
                        <SelectItem
                          key={option.value}
                          value={option.value.toString()}
                        >
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div className="flex items-center space-x-2">
                  <IconButton
                    variant="outline"
                    size="sm"
                    onClick={() => handlePageChange(pagination.page - 1)}
                    disabled={!pagination.hasPrev}
                    icon={<ChevronLeftIcon className="h-4 w-4" />}
                    label="Previous page"
                  />

                  <span className="text-sm text-muted-foreground">
                    Page {pagination.page} of {pagination.totalPages}
                  </span>

                  <IconButton
                    variant="outline"
                    size="sm"
                    onClick={() => handlePageChange(pagination.page + 1)}
                    disabled={!pagination.hasNext}
                    icon={<ChevronRightIcon className="h-4 w-4" />}
                    label="Next page"
                  />
                </div>
              </div>
            </div>
          </>
        )}
      </div>

      {/* Single Document Delete Confirmation Dialog */}
      <Dialog open={showDeleteDialog} onOpenChange={setShowDeleteDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete Document</DialogTitle>
          </DialogHeader>
          {documentToDelete && (
            <div className="space-y-4">
              <div className="flex items-center space-x-3 p-3 bg-red-50 rounded-lg">
                <TrashIcon className="h-8 w-8 text-red-600" />
                <div>
                  <p className="font-medium text-red-900">
                    Delete "{documentToDelete.title}"?
                  </p>
                  <p className="text-sm text-red-700">
                    {documentToDelete.filename} •{' '}
                    {formatFileSize(documentToDelete.file_size ?? 0)}
                  </p>
                </div>
              </div>

              <div className="space-y-2">
                <p className="text-sm text-foreground">
                  This action <strong>cannot be undone</strong>. The document
                  will be permanently deleted from:
                </p>
                <ul className="text-sm text-foreground space-y-1 ml-4">
                  <li>• Document library</li>
                  <li>• Search index</li>
                  <li>• Knowledge graph</li>
                  <li>• Vector store</li>
                </ul>
              </div>

              <div className="flex justify-end space-x-3 pt-2">
                <Button
                  variant="outline"
                  onClick={() => setShowDeleteDialog(false)}
                  disabled={isDeleting}
                >
                  Cancel
                </Button>
                <Button
                  variant="destructive"
                  onClick={handleConfirmDelete}
                  disabled={isDeleting}
                >
                  {isDeleting ? (
                    <>
                      <div className="animate-spin rounded-full h-4 w-4 border-2 border-white border-t-transparent mr-2" />
                      Deleting...
                    </>
                  ) : (
                    <>
                      <TrashIcon className="h-4 w-4 mr-2" />
                      Delete Document
                    </>
                  )}
                </Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      {/* Batch Delete Confirmation Dialog */}
      <Dialog
        open={showBatchDeleteDialog}
        onOpenChange={setShowBatchDeleteDialog}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete Multiple Documents</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div className="flex items-center space-x-3 p-3 bg-red-50 rounded-lg">
              <TrashIcon className="h-8 w-8 text-red-600" />
              <div>
                <p className="font-medium text-red-900">
                  Delete {selectedCount} documents?
                </p>
                <p className="text-sm text-red-700">
                  This action cannot be undone
                </p>
              </div>
            </div>

            {isDeleting && (
              <div className="space-y-2">
                <div className="flex justify-between text-sm">
                  <span>Deleting documents...</span>
                  <span>{deleteProgress}%</span>
                </div>
                <div className="w-full bg-muted rounded-full h-2">
                  <div
                    className="bg-red-600 h-2 rounded-full transition-all duration-300"
                    style={{ width: `${deleteProgress}%` }}
                  />
                </div>
                <p className="text-xs text-muted-foreground">
                  {Math.round((deleteProgress / 100) * selectedCount)} of{' '}
                  {selectedCount} documents deleted
                </p>
              </div>
            )}

            <div className="space-y-2">
              <p className="text-sm text-foreground">
                All selected documents will be permanently deleted from:
              </p>
              <ul className="text-sm text-foreground space-y-1 ml-4">
                <li>• Document library</li>
                <li>• Search index</li>
                <li>• Knowledge graph</li>
                <li>• Vector store</li>
              </ul>
            </div>

            <div className="flex justify-end space-x-3 pt-2">
              <Button
                variant="outline"
                onClick={() => setShowBatchDeleteDialog(false)}
                disabled={isDeleting}
              >
                Cancel
              </Button>
              <Button
                variant="destructive"
                onClick={handleConfirmBatchDelete}
                disabled={isDeleting}
              >
                {isDeleting ? (
                  <>
                    <div className="animate-spin rounded-full h-4 w-4 border-2 border-white border-t-transparent mr-2" />
                    Deleting...
                  </>
                ) : (
                  <>
                    <TrashIcon className="h-4 w-4 mr-2" />
                    Delete {selectedCount} Documents
                  </>
                )}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
};

export default DocumentLibrary;
