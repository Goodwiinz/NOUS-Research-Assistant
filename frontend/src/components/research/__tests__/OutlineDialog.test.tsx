import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@/test/test-utils';
import toast from 'react-hot-toast';
import { OutlineDialog } from '../OutlineDialog';
import type { OutlineResponse } from '@/types/scispace';

vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

const generateOutlineMock = vi.fn();
vi.mock('@/services/scispaceService', () => ({
  generateOutline: (...args: unknown[]) => generateOutlineMock(...args),
}));

const outline: OutlineResponse = {
  research_question: 'How does retrieval affect grounding?',
  style: 'academic',
  total_suggested_words: 900,
  sections: [
    {
      title: 'Introduction',
      section_type: 'introduction',
      description: 'Frame the grounding problem.',
      suggested_word_count: 400,
    },
    {
      title: 'Results',
      section_type: 'results',
      description: 'Report the retrieval ablations.',
      suggested_word_count: 500,
    },
  ],
};

// Exactly what handleCopyMarkdown is expected to hand to the clipboard.
const expectedMarkdown = [
  '## Introduction\n\nFrame the grounding problem.\n\n*Suggested: ~400 words*',
  '## Results\n\nReport the retrieval ablations.\n\n*Suggested: ~500 words*',
].join('\n\n---\n\n');

// `render` from test-utils runs userEvent.setup(), which installs its own
// navigator.clipboard stub, so the spy must be attached after render.
// Clicks go through fireEvent so nothing else touches the clipboard.
async function renderWithOutline(): Promise<{
  copyButton: HTMLElement;
  writeText: ReturnType<typeof vi.spyOn>;
}> {
  generateOutlineMock.mockResolvedValue(outline);
  render(
    <OutlineDialog isOpen onInsertOutline={vi.fn()} onClose={vi.fn()} />
  );
  fireEvent.change(
    screen.getByPlaceholderText(/What is your research question/),
    { target: { value: outline.research_question } }
  );
  fireEvent.click(screen.getByRole('button', { name: /Generate outline/ }));
  const copyButton = await waitFor(() =>
    screen.getByRole('button', { name: /Copy as Markdown/ })
  );
  const writeText = vi.spyOn(navigator.clipboard, 'writeText');
  return { copyButton, writeText };
}

describe('OutlineDialog copy as markdown', () => {
  it('reports a failed clipboard write instead of a false success', async () => {
    const { copyButton, writeText } = await renderWithOutline();
    writeText.mockRejectedValue(new DOMException('Document is not focused.'));

    fireEvent.click(copyButton);

    await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(1));
    expect(toast.error).toHaveBeenCalledWith('Could not copy the outline');
    expect(toast.success).not.toHaveBeenCalled();
    expect(writeText).toHaveBeenCalledWith(expectedMarkdown);
  });

  it('confirms success only after the markdown actually reached the clipboard', async () => {
    const { copyButton, writeText } = await renderWithOutline();
    writeText.mockResolvedValue(undefined);

    fireEvent.click(copyButton);

    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith('Outline copied as markdown')
    );
    expect(writeText).toHaveBeenCalledWith(expectedMarkdown);
    expect(toast.error).not.toHaveBeenCalled();
  });
});
