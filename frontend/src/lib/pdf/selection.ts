import {
  quotePdfSelection,
  type PdfPageRange,
  type PdfTextSelection,
} from '@/types/pdf-chat';

/** Page identity comes from this viewer's real text-layer endpoints only. */
export function selectedPdfPages(
  root: HTMLElement,
  selection: Selection | null
): PdfPageRange | undefined {
  if (!selection || selection.isCollapsed || !selection.toString().trim())
    return;
  const pageIndex = (node: Node | null): number | undefined => {
    const element = node instanceof Element ? node : node?.parentElement;
    const layer = element?.closest<HTMLElement>('[data-pdf-text-page]');
    if (!layer || !root.contains(layer)) return;
    if (!/^\d+$/.test(layer.dataset.pdfTextPage ?? '')) return;
    const index = Number(layer.dataset.pdfTextPage);
    return Number.isSafeInteger(index) && index >= 0 ? index : undefined;
  };
  const anchor = pageIndex(selection.anchorNode);
  const focus = pageIndex(selection.focusNode);
  if (anchor === undefined || focus === undefined) return;
  return [Math.min(anchor, focus), Math.max(anchor, focus)];
}

/** Extract only the intersected PDF text layers, excluding viewer controls. */
export function selectedPdfText(
  root: HTMLElement,
  selection: Selection | null
): PdfTextSelection | undefined {
  const range = selectedPdfPages(root, selection);
  if (!range || !selection || selection.rangeCount !== 1) return;
  const selected = selection.getRangeAt(0);
  const fragments: string[] = [];
  for (const layer of root.querySelectorAll<HTMLElement>(
    '[data-pdf-text-page]'
  )) {
    if (!selected.intersectsNode(layer)) continue;
    const intersection = document.createRange();
    intersection.selectNodeContents(layer);
    if (layer.contains(selected.startContainer))
      intersection.setStart(selected.startContainer, selected.startOffset);
    if (layer.contains(selected.endContainer))
      intersection.setEnd(selected.endContainer, selected.endOffset);
    const text = (node: Node): string => {
      if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? '';
      if (node instanceof HTMLBRElement) return '\n';
      return Array.from(node.childNodes, text).join('');
    };
    fragments.push(text(intersection.cloneContents()));
  }
  const text = fragments.join('\n');
  return text.trim() ? { range, quote: quotePdfSelection(text) } : undefined;
}
