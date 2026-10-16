'use client';

import React, { useState } from 'react';
import { ListTree, Loader2, Copy, Check } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { generateOutline } from '@/services/scispaceService';
import type {
  OutlineResponse,
  OutlineSection,
  SectionType,
} from '@/types/scispace';
import toast from 'react-hot-toast';

interface OutlineDialogProps {
  isOpen: boolean;
  documents?: Array<{ id: string; title: string }>;
  onInsertOutline: (sections: OutlineSection[]) => void;
  onClose: () => void;
}

const styleOptions = [
  { value: 'academic' as const, label: 'Academic' },
  { value: 'technical' as const, label: 'Technical' },
  { value: 'summary' as const, label: 'Summary' },
];

const sectionTypeOptions: { value: SectionType; label: string }[] = [
  { value: 'introduction', label: 'Introduction' },
  { value: 'methodology', label: 'Methodology' },
  { value: 'results', label: 'Results' },
  { value: 'discussion', label: 'Discussion' },
  { value: 'conclusion', label: 'Conclusion' },
  { value: 'abstract', label: 'Abstract' },
];

export const OutlineDialog: React.FC<OutlineDialogProps> = ({
  isOpen,
  documents = [],
  onInsertOutline,
  onClose,
}) => {
  const [question, setQuestion] = useState('');
  const [style, setStyle] = useState<'academic' | 'technical' | 'summary'>(
    'academic'
  );
  const [selectedDocIds, setSelectedDocIds] = useState<string[]>([]);
  const [selectedSections, setSelectedSections] = useState<SectionType[]>([]);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<OutlineResponse | null>(null);

  const toggleDocId = (id: string) => {
    setSelectedDocIds((prev) =>
      prev.includes(id) ? prev.filter((d) => d !== id) : [...prev, id]
    );
  };

  const toggleSectionType = (type: SectionType) => {
    setSelectedSections((prev) =>
      prev.includes(type) ? prev.filter((s) => s !== type) : [...prev, type]
    );
  };

  const handleGenerate = async () => {
    if (!question.trim() || question.trim().length < 10) return;
    setLoading(true);
    try {
      const res = await generateOutline({
        research_question: question.trim(),
        style,
        document_ids: selectedDocIds.length > 0 ? selectedDocIds : undefined,
        section_types:
          selectedSections.length > 0 ? selectedSections : undefined,
      });
      setResult(res);
    } catch {
      toast.error('Failed to generate outline');
    } finally {
      setLoading(false);
    }
  };

  const handleCopyMarkdown = async () => {
    if (!result) return;
    const md = result.sections
      .map(
        (s) =>
          `## ${s.title}\n\n${s.description}\n\n*Suggested: ~${s.suggested_word_count} words*`
      )
      .join('\n\n---\n\n');
    try {
      await navigator.clipboard.writeText(md);
      toast.success('Outline copied as markdown');
    } catch {
      // Clipboard access denied, document unfocused, or insecure context.
      toast.error('Could not copy the outline');
    }
  };

  const handleOpenChange = (open: boolean) => {
    if (!open) {
      setResult(null);
      setQuestion('');
      setSelectedDocIds([]);
      setSelectedSections([]);
      onClose();
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-2xl max-h-[85vh] flex flex-col">
        <DialogHeader className="shrink-0">
          <DialogTitle className="flex items-center gap-2">
            <ListTree className="h-5 w-5 text-primary" />
            Generate outline
          </DialogTitle>
          <DialogDescription>
            Create a structured outline for your research document
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 overflow-y-auto flex-1">
          {/* Research Question */}
          <div>
            <label className="mb-1 block text-sm font-medium text-foreground">
              Research question <span className="text-destructive">*</span>
            </label>
            <textarea
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="What is your research question? (min 10 characters)"
              rows={3}
              className="w-full resize-none rounded border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>

          {/* Style Selector */}
          <div>
            <label className="mb-1 block text-sm font-medium text-foreground">
              Style
            </label>
            <div className="flex gap-2">
              {styleOptions.map((opt) => (
                <button
                  key={opt.value}
                  onClick={() => setStyle(opt.value)}
                  className={cn(
                    'rounded border px-3 py-1.5 text-xs transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring',
                    style === opt.value
                      ? 'border-primary/50 bg-primary/10 text-primary'
                      : 'border-border text-muted-foreground hover:border-primary/30'
                  )}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </div>

          {/* Section Types */}
          <div>
            <label className="mb-1 block text-sm font-medium text-foreground">
              Sections <span className="text-muted-foreground">(optional)</span>
            </label>
            <div className="flex flex-wrap gap-2">
              {sectionTypeOptions.map((opt) => (
                <label
                  key={opt.value}
                  className="flex cursor-pointer items-center gap-1.5"
                >
                  <input
                    type="checkbox"
                    checked={selectedSections.includes(opt.value)}
                    onChange={() => toggleSectionType(opt.value)}
                    className="accent-primary"
                  />
                  <span className="text-xs text-muted-foreground">
                    {opt.label}
                  </span>
                </label>
              ))}
            </div>
          </div>

          {/* Document Scope */}
          {documents.length > 0 && (
            <div>
              <label className="mb-2 block text-sm font-medium text-foreground">
                Document scope <span className="text-muted-foreground">(optional)</span>
              </label>
              <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                {documents.map((doc) => (
                  <label
                    key={doc.id}
                    className="flex items-center gap-2 rounded border border-border px-3 py-2 text-sm text-foreground"
                  >
                    <input
                      type="checkbox"
                      checked={selectedDocIds.includes(doc.id)}
                      onChange={() => toggleDocId(doc.id)}
                      className="accent-primary"
                    />
                    <span className="truncate text-xs">
                      {doc.title}
                    </span>
                  </label>
                ))}
              </div>
            </div>
          )}

          {/* Generate Button */}
          {!result && (
            <Button
              onClick={handleGenerate}
              disabled={loading || question.trim().length < 10}
              className="w-full gap-2"
            >
              {loading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <ListTree className="h-4 w-4" />
              )}
              {loading ? 'Generating...' : 'Generate outline'}
            </Button>
          )}

          {/* Result */}
          {result && (
            <div className="space-y-3 rounded-lg border border-border bg-muted p-4">
              <div className="flex items-center justify-between">
                <span className="text-xs text-muted-foreground">
                  {result.sections.length} sections ~
                  {result.total_suggested_words.toLocaleString()} words
                </span>
              </div>
              {result.sections.map((section, idx) => (
                <div
                  key={idx}
                  className="rounded border border-border bg-card p-3"
                >
                  <div className="flex items-center justify-between">
                    <h4 className="text-sm font-semibold text-foreground">
                      {section.title}
                    </h4>
                    <span className="text-[10px] text-muted-foreground">
                      ~{section.suggested_word_count} words
                    </span>
                  </div>
                  <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                    {section.description}
                  </p>
                </div>
              ))}
            </div>
          )}
        </div>

        <DialogFooter className="shrink-0">
          <Button
            variant="ghost"
            size="sm"
            className="text-muted-foreground hover:bg-muted hover:text-foreground"
            onClick={handleOpenChange.bind(null, false)}
          >
            Cancel
          </Button>
          {result && (
            <>
              <Button
                variant="ghost"
                size="sm"
                className="gap-1.5 text-primary hover:bg-primary/10 hover:text-primary"
                onClick={handleCopyMarkdown}
              >
                <Copy className="h-3.5 w-3.5" />
                Copy as Markdown
              </Button>
              <Button
                size="sm"
                className="gap-1.5 bg-primary/10 text-primary hover:bg-primary/20"
                onClick={() => {
                  onInsertOutline(result.sections);
                  handleOpenChange(false);
                }}
              >
                <Check className="h-3.5 w-3.5" />
                Insert outline
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};

export default OutlineDialog;
