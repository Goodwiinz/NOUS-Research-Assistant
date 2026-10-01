'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Download, Pencil, Play, Trash2, X } from 'lucide-react';
import type {
  ExtractionColumn,
  ExtractionMatrix as ExtractionMatrixType,
  ExtractionTaskStatus,
} from '@/types/scispace';
import {
  createMatrix,
  deleteMatrix,
  getExtractionTaskStatus,
  getMatrix,
  listMatrices,
  triggerExtraction,
  updateMatrix,
} from '@/services/scispaceService';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { ColumnEditor } from './ColumnEditor';
import { Badge } from '@/components/ui/badge';
import { CellCitation } from './CellCitation';
import { CellObservations, MISSINGNESS_LABELS } from './CellObservations';

const CELL_BADGE = 'px-1.5 py-0 text-[10px]';

interface ExtractionMatrixProps {
  projectId: string;
  matrixId?: string;
  documents?: Array<{ id: string; title: string }>;
}

export function ExtractionMatrix({
  projectId,
  matrixId,
  documents = [],
}: ExtractionMatrixProps) {
  const [matrix, setMatrix] = useState<ExtractionMatrixType | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [extracting, setExtracting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  const [createName, setCreateName] = useState('');
  const [createColumns, setCreateColumns] = useState<ExtractionColumn[]>([
    { name: '', description: '' },
  ]);
  const [creating, setCreating] = useState(false);

  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState('');
  const [editColumns, setEditColumns] = useState<ExtractionColumn[]>([]);
  const [saving, setSaving] = useState(false);

  const [autoExtractionTaskId, setAutoExtractionTaskId] = useState<
    string | null
  >(null);
  const [autoExtractionStatus, setAutoExtractionStatus] =
    useState<ExtractionTaskStatus | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchMatrix = useCallback(async (id: string) => {
    setLoading(true);
    setError(null);
    try {
      const data = await getMatrix(id);
      setMatrix(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load matrix');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (matrixId) {
      // Pre-existing fetch-on-mount (not TanStack yet); the loading flip is intended.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      fetchMatrix(matrixId);
    } else {
      setLoading(true);
      listMatrices(projectId)
        .then((data) => {
          if (data.matrices.length > 0) {
            fetchMatrix(data.matrices[0].id);
          }
        })
        .catch(() => {})
        .finally(() => setLoading(false));
    }
  }, [matrixId, projectId, fetchMatrix]);

  useEffect(() => {
    if (!autoExtractionTaskId) return;

    const poll = async () => {
      try {
        const status = await getExtractionTaskStatus(autoExtractionTaskId);
        setAutoExtractionStatus(status);

        if (status.status === 'completed' || status.status === 'failed') {
          if (pollRef.current) {
            clearInterval(pollRef.current);
            pollRef.current = null;
          }
          setAutoExtractionTaskId(null);

          if (status.status === 'completed' && matrix) {
            await fetchMatrix(matrix.id);
          }
        }
      } catch {
        // Task may not be registered yet, keep polling
      }
    };

    pollRef.current = setInterval(poll, 3000);
    poll();

    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [autoExtractionTaskId, matrix, fetchMatrix]);

  const handleCreate = async () => {
    const validColumns = createColumns.filter((c) => c.name.trim());
    if (!createName.trim() || validColumns.length === 0) return;

    setCreating(true);
    setError(null);
    try {
      const result = await createMatrix(projectId, {
        name: createName.trim(),
        columns: validColumns.map((c) => ({
          name: c.name.trim(),
          description: c.description?.trim() || undefined,
        })),
      });
      await fetchMatrix(result.id);

      if (result.extraction_task_id) {
        setAutoExtractionTaskId(result.extraction_task_id);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create matrix');
    } finally {
      setCreating(false);
    }
  };

  const handleExtract = async () => {
    if (!matrix || documents.length === 0) return;

    setExtracting(true);
    setError(null);
    try {
      await triggerExtraction(matrix.id, {
        document_ids: documents.map((d) => d.id),
      });
      await fetchMatrix(matrix.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Extraction failed');
    } finally {
      setExtracting(false);
    }
  };

  const handleDelete = async () => {
    if (!matrix) return;

    setDeleting(true);
    setError(null);
    try {
      await deleteMatrix(matrix.id);
      setMatrix(null);
      setDeleteOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete matrix');
    } finally {
      setDeleting(false);
    }
  };

  const handleStartEdit = () => {
    if (!matrix) return;
    setEditName(matrix.name);
    setEditColumns(matrix.columns.map((c) => ({ ...c })));
    setEditing(true);
  };

  const handleCancelEdit = () => {
    setEditing(false);
    setEditName('');
    setEditColumns([]);
  };

  const handleSaveEdit = async () => {
    if (!matrix) return;

    const validColumns = editColumns.filter((c) => c.name.trim());
    if (!editName.trim() || validColumns.length === 0) return;

    setSaving(true);
    setError(null);
    try {
      const result = await updateMatrix(matrix.id, {
        name: editName.trim(),
        columns: validColumns.map((c) => ({
          name: c.name.trim(),
          description: c.description?.trim() || undefined,
        })),
        clear_stale_cells: true,
      });

      setEditing(false);
      await fetchMatrix(matrix.id);

      if (result.columns_changed && documents.length > 0) {
        try {
          await triggerExtraction(matrix.id, {
            document_ids: documents.map((d) => d.id),
          });
          await fetchMatrix(matrix.id);
        } catch {
          // Non-critical
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update matrix');
    } finally {
      setSaving(false);
    }
  };

  const handleExportCsv = () => {
    if (!matrix) return;

    const headers = ['Document', ...matrix.columns.map((c) => c.name)];
    const rows = documents.map((doc) => {
      const docCells = matrix.columns.map((col) => {
        const cell = matrix.cells.find(
          (c) => c.document_id === doc.id && c.column_name === col.name
        );
        return cell?.value ?? '';
      });
      return [doc.title, ...docCells];
    });

    const escape = (val: string) => {
      if (val.includes(',') || val.includes('"') || val.includes('\n')) {
        return `"${val.replace(/"/g, '""')}"`;
      }
      return val;
    };

    const csv = [
      headers.map(escape).join(','),
      ...rows.map((r) => r.map(escape).join(',')),
    ].join('\n');

    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${matrix.name.replace(/\s+/g, '_')}_extraction.csv`;
    link.click();
    URL.revokeObjectURL(url);
  };

  const getCellValue = (documentId: string, columnName: string) => {
    return (
      matrix?.cells.find(
        (c) => c.document_id === documentId && c.column_name === columnName
      ) ?? null
    );
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16 gap-2">
        <span className="text-sm text-muted-foreground">Loading matrix…</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-4">
        <p className="text-sm text-destructive">{error}</p>
        {matrixId && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => fetchMatrix(matrixId)}
            className="mt-3"
          >
            Retry
          </Button>
        )}
      </div>
    );
  }

  if (!matrix && !matrixId) {
    return (
      <div className="space-y-6 rounded-lg border border-border bg-card p-6">
        <h3 className="text-base font-semibold text-foreground mb-4">
          Create extraction matrix
        </h3>
        <div className="space-y-4">
          <div>
            <Label className="mb-1.5 block">Matrix name</Label>
            <Input
              value={createName}
              onChange={(e) => setCreateName(e.target.value)}
              placeholder="e.g. Literature Review 2024"
            />
          </div>

          <ColumnEditor columns={createColumns} onChange={setCreateColumns} />

          <Button
            variant="outline"
            disabled={
              creating ||
              !createName.trim() ||
              createColumns.filter((c) => c.name.trim()).length === 0
            }
            isLoading={creating}
            loadingText="Creating…"
            onClick={handleCreate}
          >
            Create matrix
          </Button>
        </div>
      </div>
    );
  }

  if (!matrix) return null;

  return (
    <div className="space-y-4">
      {autoExtractionTaskId && autoExtractionStatus && (
        <div className="flex items-center gap-2 rounded-lg border border-border bg-card px-4 py-2">
          <span className="text-xs text-muted-foreground">
            Extracting {autoExtractionStatus.completed}/
            {autoExtractionStatus.total}…
          </span>
          {autoExtractionStatus.failed > 0 && (
            <span className="text-xs text-destructive">
              ({autoExtractionStatus.failed} failed)
            </span>
          )}
        </div>
      )}

      <div className="flex items-center justify-between rounded-lg border border-border bg-card px-4 py-3">
        {editing ? (
          <div className="flex-1 mr-4">
            <Input
              value={editName}
              onChange={(e) => setEditName(e.target.value)}
            />
          </div>
        ) : (
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-medium text-foreground">
              {matrix.name}
            </h3>
            <Badge
              variant="outline"
              className={CELL_BADGE}
              title={
                matrix.form_version
                  ? `Form hash ${matrix.form_version.content_hash.slice(0, 12)}…` +
                    (matrix.form_version.protocol_version_id
                      ? ` · protocol version ${matrix.form_version.protocol_version_id}`
                      : '')
                  : undefined
              }
            >
              {matrix.form_version &&
              matrix.form_version.provenance !== 'legacy_unversioned'
                ? `Form v${matrix.form_version.version_no}`
                : 'Legacy (unversioned)'}
            </Badge>
          </div>
        )}
        <div className="flex items-center gap-2">
          {editing ? (
            <>
              <Button
                variant="outline"
                size="sm"
                disabled={
                  saving ||
                  !editName.trim() ||
                  editColumns.filter((c) => c.name.trim()).length === 0
                }
                isLoading={saving}
                loadingText="Saving…"
                onClick={handleSaveEdit}
              >
                Save
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={handleCancelEdit}
                disabled={saving}
              >
                <X className="h-3 w-3 mr-1" />
                Cancel
              </Button>
            </>
          ) : (
            <>
              <Button variant="outline" size="sm" onClick={handleStartEdit}>
                <Pencil className="h-3 w-3 mr-1" />
                Edit
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={extracting || documents.length === 0}
                isLoading={extracting}
                loadingText="Extracting…"
                onClick={handleExtract}
              >
                <Play className="h-3 w-3 mr-1" />
                Extract
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={handleExportCsv}
                disabled={matrix.cells.length === 0}
              >
                <Download className="h-3 w-3 mr-1" />
                Export CSV
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setDeleteOpen(true)}
              >
                <Trash2 className="h-3 w-3 mr-1 text-destructive" />
                Delete
              </Button>
            </>
          )}
        </div>
      </div>

      {editing && (
        <div className="rounded-lg border border-border bg-card p-4">
          <ColumnEditor columns={editColumns} onChange={setEditColumns} />
        </div>
      )}

      <div className="rounded-lg border border-border overflow-hidden">
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow className="border-border hover:bg-transparent">
                <TableHead className="text-xs font-medium text-muted-foreground bg-card min-w-[200px]">
                  Document
                </TableHead>
                {matrix.columns.map((col) => (
                  <TableHead
                    key={col.name}
                    className="text-xs font-medium text-muted-foreground bg-card min-w-[150px]"
                    title={col.description}
                  >
                    {col.name}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {documents.length === 0 ? (
                <TableRow className="border-border">
                  <TableCell
                    colSpan={matrix.columns.length + 1}
                    className="text-center text-sm text-muted-foreground py-8"
                  >
                    No documents available
                  </TableCell>
                </TableRow>
              ) : (
                documents.map((doc, idx) => (
                  <TableRow
                    key={doc.id}
                    className={`border-border hover:bg-muted transition-colors ${idx % 2 === 1 ? 'bg-muted/20' : ''}`}
                  >
                    <TableCell className="text-sm text-foreground font-medium">
                      {doc.title}
                    </TableCell>
                    {matrix.columns.map((col) => {
                      const cell = getCellValue(doc.id, col.name);
                      return (
                        <TableCell
                          key={col.name}
                          className="text-sm text-muted-foreground"
                        >
                          <div className="flex items-start gap-1.5">
                            {cell?.missingness ? (
                              <span className="flex-1 italic text-muted-foreground">
                                {MISSINGNESS_LABELS[cell.missingness] ??
                                  cell.missingness}
                              </span>
                            ) : (
                              <span className="flex-1">
                                {cell?.value ?? ''}
                              </span>
                            )}
                            {cell?.stale && (
                              <Badge
                                variant="warning"
                                className={CELL_BADGE}
                                aria-label="Stale: the form field or source document changed"
                              >
                                Stale
                              </Badge>
                            )}
                            {cell?.source === 'legacy' && (
                              <Badge variant="info" className={CELL_BADGE}>
                                Legacy
                              </Badge>
                            )}
                            {cell?.validation_state === 'invalid' && (
                              <Badge variant="warning" className={CELL_BADGE}>
                                Unvalidated
                              </Badge>
                            )}
                            {(cell?.observed_values ?? 0) > 1 && (
                              <Badge variant="warning" className={CELL_BADGE}>
                                {cell?.observed_values} values
                              </Badge>
                            )}
                            {cell?.field_id ? (
                              <CellObservations
                                projectId={projectId}
                                matrixId={matrix.id}
                                documentId={doc.id}
                                fieldId={cell.field_id}
                                column={col.name}
                                citation={cell.citation_snippet}
                                anchorStatus={cell.anchor_status}
                                onDecided={() => fetchMatrix(matrix.id)}
                              />
                            ) : (
                              <CellCitation
                                citation_snippet={
                                  cell?.citation_snippet ?? null
                                }
                                anchorStatus={cell?.anchor_status}
                              />
                            )}
                          </div>
                        </TableCell>
                      );
                    })}
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </div>
      </div>

      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete matrix</DialogTitle>
            <DialogDescription>
              This will permanently delete &quot;{matrix.name}&quot; and all
              extracted data. This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setDeleteOpen(false)}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              size="sm"
              disabled={deleting}
              isLoading={deleting}
              loadingText="Deleting…"
              onClick={handleDelete}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export default ExtractionMatrix;
