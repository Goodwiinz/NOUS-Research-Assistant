/** Resubmitting a turn must retain its attached documents. */
import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useChatComposerActions } from '@/hooks/chat/useChatComposerActions';
import { makeChatPageMessage } from '@/test/chatMessageFactory';

vi.mock('react-hot-toast', () => ({ default: { error: vi.fn() } }));
vi.mock('@/services/enhancedDocumentService', () => ({
  enhancedDocumentService: { uploadDocument: vi.fn() },
}));

const documentId = '11111111-1111-4111-8111-111111111111';
const clientMessageId = '22222222-2222-4222-8222-222222222222';

function setup(): {
  result: { current: ReturnType<typeof useChatComposerActions> };
  handleSubmit: ReturnType<typeof vi.fn>;
} {
  const handleSubmit = vi.fn().mockResolvedValue(undefined);
  const { result } = renderHook(() =>
    useChatComposerActions({
      workspace: null,
      setInput: vi.fn(),
      handleSubmit,
      isLoading: false,
      storeIsStreaming: false,
      displayedMessages: [
        makeChatPageMessage({
          id: 'user-row',
          role: 'user',
          content: 'Summarize the attached paper',
          clientMessageId,
          attachments: [
            {
              id: 'attachment-row',
              document_id: documentId,
              display_name: 'Synthetic audit paper.pdf',
            },
          ],
        }),
        makeChatPageMessage({
          id: 'answer-row',
          role: 'assistant',
          content: 'Summary',
        }),
      ],
    })
  );
  return { result, handleSubmit };
}

afterEach(() => vi.useRealTimers());

describe('attached chat turns', () => {
  it('control: an ordinary submit forwards document IDs', () => {
    const { result, handleSubmit } = setup();
    act(() => result.current.submit([documentId]));
    expect(handleSubmit).toHaveBeenCalledWith(undefined, undefined, undefined, [
      documentId,
    ]);
  });

  it.each(['edit', 'regenerate'] as const)(
    '%s retains the original document IDs',
    (action) => {
      vi.useFakeTimers();
      const { result, handleSubmit } = setup();
      const content =
        action === 'edit'
          ? 'Summarize its methods'
          : 'Summarize the attached paper';
      act(() => {
        if (action === 'edit') result.current.handleEditUserMessage(0, content);
        else result.current.handleRegenerate(1);
        vi.runAllTimers();
      });
      expect(handleSubmit).toHaveBeenCalledWith(content, [], clientMessageId, [
        documentId,
      ]);
    }
  );
});
