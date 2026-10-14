'use client';

import { useState } from 'react';
import { PdfDocumentViewer } from '@/components/documents/PdfDocumentViewer';
import type { PdfPageRange } from '@/types/pdf-chat';

/** Authored, ASCII-only PDF fixture; no document download or provider needed. */
function selectionPdf(): Blob {
  const labels = [
    'First page evidence',
    'Second page method',
    'Third page conclusion',
  ];
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [4 0 R 6 0 R 8 0 R] /Count 3 >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
  ];
  labels.forEach((label, index) => {
    const stream = `BT /F1 16 Tf 30 180 Td (${label}) Tj 0 -32 Td (Select this text to quote the passage.) Tj ET`;
    objects.push(
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 420 240] /Resources << /Font << /F1 3 0 R >> >> /Contents ${5 + index * 2} 0 R >>`,
      `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`
    );
  });
  let pdf = '%PDF-1.4\n';
  const offsets = [0];
  objects.forEach((object, index) => {
    offsets.push(pdf.length);
    pdf += `${index + 1} 0 obj\n${object}\nendobj\n`;
  });
  const xref = pdf.length;
  pdf += `xref\n0 ${offsets.length}\n0000000000 65535 f \n`;
  pdf += offsets
    .slice(1)
    .map((offset) => `${String(offset).padStart(10, '0')} 00000 n \n`)
    .join('');
  pdf += `trailer\n<< /Size ${offsets.length} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF`;
  return new Blob([pdf], { type: 'application/pdf' });
}

export function PdfSelectionFixture(): React.ReactElement {
  const [blob, setBlob] = useState(selectionPdf);
  const [page, setPage] = useState(1);
  const [submitted, setSubmitted] = useState<PdfPageRange | 'whole' | null>(
    null
  );
  return (
    <main className="mx-auto max-w-3xl space-y-3 p-4">
      <h1>PDF selection verification</h1>
      <p>
        This synthetic document checks rendering and page selection without a
        model request.
      </p>
      <div className="flex gap-3">
        <button type="button" onClick={() => setPage(page === 3 ? 1 : 3)}>
          Open cited page {page === 3 ? 1 : 3}
        </button>
        <button
          type="button"
          onClick={() => {
            setBlob(selectionPdf());
            setSubmitted(null);
          }}
        >
          Replace document
        </button>
      </div>
      <output aria-label="Submitted PDF scope">
        {submitted === null
          ? 'No question submitted'
          : submitted === 'whole'
            ? 'Whole PDF'
            : `Zero-based pages: ${submitted[0]}, ${submitted[1]}`}
      </output>
      <PdfDocumentViewer
        blob={blob}
        title="Selection fixture"
        targetPage={page}
        onAsk={(range) => setSubmitted(range ?? 'whole')}
      />
    </main>
  );
}
