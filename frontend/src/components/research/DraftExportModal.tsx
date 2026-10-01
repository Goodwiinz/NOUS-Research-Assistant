'use client';

/**
 * DraftExportModal Component
 * Modal for exporting drafts to different formats
 */

import React, { useState } from 'react';
import { X, Download, FileText, Code, Loader2, BookOpen } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Switch } from '@/components/ui/switch';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import type { DraftExportFormat } from '@/services/projectService';

/** GOO-317: references-only files; the bibliography options do not apply. */
const REFERENCE_FILES: {
  value: DraftExportFormat;
  label: string;
  ext: string;
}[] = [
  { value: 'csl-json', label: 'CSL JSON', ext: 'references.json' },
  { value: 'ris', label: 'RIS', ext: 'references.ris' },
];

const optionClass = (selected: boolean): string =>
  `flex flex-col items-center gap-2 p-4 rounded-lg border transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring ${
    selected
      ? 'bg-primary/10 border-primary/50 text-primary'
      : 'bg-muted border-border text-muted-foreground hover:border-muted-foreground/50'
  }`;

export interface DraftExportModalProps {
  isOpen: boolean;
  onClose: () => void;
  onExport: (
    format: DraftExportFormat,
    includeBibliography: boolean,
    bibliographyFormat: 'bibtex' | 'biblatex'
  ) => Promise<void>;
  draftTitle: string;
  initialFormat?: DraftExportFormat;
}

export const DraftExportModal: React.FC<DraftExportModalProps> = ({
  isOpen,
  onClose,
  onExport,
  draftTitle,
  initialFormat = 'markdown',
}) => {
  const [format, setFormat] = useState<DraftExportFormat>(initialFormat);
  const referencesOnly = format === 'csl-json' || format === 'ris';
  const [includeBibliography, setIncludeBibliography] = useState(true);
  const [bibliographyFormat, setBibliographyFormat] = useState<
    'bibtex' | 'biblatex'
  >('bibtex');
  const [exporting, setExporting] = useState(false);

  // Reset the format whenever the dialog opens (or its initial format
  // changes): adjusted during render rather than in an effect.
  const [synced, setSynced] = useState({ isOpen, initialFormat });
  if (synced.isOpen !== isOpen || synced.initialFormat !== initialFormat) {
    setSynced({ isOpen, initialFormat });
    if (isOpen) setFormat(initialFormat);
  }

  const handleExport = async () => {
    setExporting(true);
    try {
      await onExport(format, includeBibliography, bibliographyFormat);
      onClose();
    } catch (error) {
      console.error('Export failed:', error);
    } finally {
      setExporting(false);
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Export draft</DialogTitle>
          <DialogDescription>
            Exporting: {draftTitle}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div>
            <span className="text-sm font-medium block mb-2">Format</span>
            <div className="grid grid-cols-2 gap-3">
              <button
                type="button"
                onClick={() => setFormat('markdown')}
                className={`flex flex-col items-center gap-2 p-4 rounded-lg border transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring ${
                  format === 'markdown'
                    ? 'bg-primary/10 border-primary/50 text-primary'
                    : 'bg-muted border-border text-muted-foreground hover:border-muted-foreground/50'
                }`}
              >
                <FileText className="h-6 w-6" />
                <span className="text-sm font-medium">Markdown</span>
                <span className="text-xs text-muted-foreground">.md</span>
              </button>
              <button
                type="button"
                onClick={() => setFormat('latex')}
                className={`flex flex-col items-center gap-2 p-4 rounded-lg border transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring ${
                  format === 'latex'
                    ? 'bg-primary/10 border-primary/50 text-primary'
                    : 'bg-muted border-border text-muted-foreground hover:border-muted-foreground/50'
                }`}
              >
                <Code className="h-6 w-6" />
                <span className="text-sm font-medium">LaTeX</span>
                <span className="text-xs text-muted-foreground">
                  .tex + .bib
                </span>
              </button>
            </div>
          </div>

          <div>
            <span className="text-sm font-medium block mb-2">
              Reference file
            </span>
            <div className="grid grid-cols-2 gap-3">
              {REFERENCE_FILES.map(({ value, label, ext }) => (
                <button
                  key={value}
                  type="button"
                  aria-pressed={format === value}
                  onClick={() => setFormat(value)}
                  className={optionClass(format === value)}
                >
                  <BookOpen className="h-6 w-6" />
                  <span className="text-sm font-medium">{label}</span>
                  <span className="text-xs text-muted-foreground">{ext}</span>
                </button>
              ))}
            </div>
          </div>

          {referencesOnly ? (
            <div className="p-3 bg-muted rounded text-xs text-muted-foreground">
              References only, keyed doc1, doc2, … as cited in the draft.
              Missing metadata is left out, never filled in.
            </div>
          ) : (
            <div className="flex items-center justify-between p-3 bg-muted rounded-lg">
              <div>
                <span className="text-sm">Include bibliography</span>
                <p className="text-xs text-muted-foreground mt-0.5">
                  {format === 'latex'
                    ? 'Export references.bib file'
                    : 'Add references section'}
                </p>
              </div>
              <Switch
                checked={includeBibliography}
                onCheckedChange={setIncludeBibliography}
                aria-label="Include bibliography"
              />
            </div>
          )}

          {includeBibliography && !referencesOnly && (
            <div>
              <span className="text-sm font-medium block mb-1.5">
                Bibliography format
              </span>
              <Select
                value={bibliographyFormat}
                onValueChange={(v) =>
                  setBibliographyFormat(v as 'bibtex' | 'biblatex')
                }
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="bibtex">BibTeX</SelectItem>
                  <SelectItem value="biblatex">BibLaTeX</SelectItem>
                </SelectContent>
              </Select>
            </div>
          )}

          {format === 'latex' && (
            <div className="p-3 bg-muted rounded text-xs text-muted-foreground">
              LaTeX export includes a .tex file and references.bib. Compile with
              pdflatex + bibtex.
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={exporting}>
            Cancel
          </Button>
          <Button onClick={handleExport} disabled={exporting}>
            {exporting && (
              <Loader2 className="h-4 w-4 animate-spin mr-2" />
            )}
            Export
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};

export default DraftExportModal;
