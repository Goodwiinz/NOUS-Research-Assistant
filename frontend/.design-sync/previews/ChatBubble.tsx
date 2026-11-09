import { ChatBubble } from '@nous/design-system';
import { DarkSurface } from './_DarkSurface';

// ChatBubble renders one turn of the research chat. Assistant prose goes
// through the citation renderer, so `[Doc N]` markers in `content` become
// footnote superscripts resolved against `citations` (1-based), and the
// referenced sources appear as chips under the answer.

const T = Date.UTC(2026, 10, 3, 14, 32);

const citations = [
  {
    documentId: 'doc-flash-attn',
    chunkId: 'chunk-0012',
    title: 'FlashAttention: Fast and Memory-Efficient Exact Attention with IO-Awareness',
    score: 0.94,
    source: 'flash_attn.pdf',
    pageNumber: 3,
    content:
      'We propose FlashAttention, an IO-aware exact attention algorithm that uses tiling to reduce the number of memory reads/writes between GPU HBM and on-chip SRAM.',
  },
  {
    documentId: 'doc-longformer',
    chunkId: 'chunk-0044',
    title: 'Longformer: The Long-Document Transformer',
    score: 0.81,
    source: 'longformer.pdf',
    pageNumber: 5,
  },
  {
    externalReferenceId: 'arXiv:2001.04451',
    title: 'Reformer: The Efficient Transformer',
    score: 0.66,
    source: 'arXiv',
  },
];

const answer = {
  id: 'msg-assistant-1',
  role: 'assistant' as const,
  timestamp: T + 42_000,
  citations,
  content: `Exact attention can be made fast without approximation. **FlashAttention** tiles the computation so the full $n \\times n$ score matrix never leaves on-chip SRAM, cutting HBM traffic by up to 9× [Doc 1].

Approximate methods trade exactness for asymptotic gains:

- **Sliding-window + global tokens** reduce cost to linear in sequence length [Doc 2].
- **Locality-sensitive hashing** buckets similar keys, giving $O(n \\log n)$ [Doc 3].

For sequences under 2k tokens the tiled exact kernel is usually the better default.`,
  metadata: {
    toolsUsed: ['search_documents', 'search_arxiv'],
    responseTimeMs: 6840,
    ttftMs: 1210,
    sourcesCount: 3,
    tokenUsage: { input: 5120, output: 412 },
  },
};

// Shorter committed answer for the multi-bubble and plan stories.
const shortAnswer = {
  ...answer,
  id: 'msg-assistant-short',
  citations: citations.slice(0, 2),
  content:
    'FlashAttention keeps attention exact but tiles it so the score matrix never leaves SRAM [Doc 1]; Longformer instead drops to linear cost with a sliding window plus global tokens [Doc 2].',
  metadata: { ...answer.metadata, sourcesCount: 2 },
};

const question = {
  id: 'msg-user-1',
  role: 'user' as const,
  timestamp: T,
  content:
    'How do FlashAttention and the sparse-attention papers in my library compare for long-context training?',
};

export const AssistantAnswer = () => (
  <div style={{ maxWidth: 760 }}>
    <ChatBubble message={answer} index={1} modelName="GPT-5" onRetry={() => {}} />
  </div>
);

export const UserMessage = () => (
  <div style={{ maxWidth: 760 }}>
    <ChatBubble message={question} index={0} />
  </div>
);

export const Conversation = () => (
  <div style={{ maxWidth: 760 }}>
    <ChatBubble message={question} index={0} />
    <ChatBubble message={shortAnswer} index={1} modelName="GPT-5" />
  </div>
);

export const WithExecutionPlan = () => (
  <div style={{ maxWidth: 760 }}>
    <ChatBubble
      index={1}
      modelName="GPT-5"
      message={{
        ...shortAnswer,
        id: 'msg-assistant-plan',
        // One source + one tool row keeps the expanded plan inside the cell.
        citations: citations.slice(0, 1),
        content:
          'FlashAttention keeps attention exact but tiles it so the score matrix never leaves SRAM [Doc 1].',
        metadata: { ...shortAnswer.metadata, sourcesCount: 1 },
        planReasoning:
          'Check the user library first, then fill gaps from arXiv before synthesizing.',
        plan: [
          {
            step: 1,
            description: 'Search the project library for attention-efficiency papers',
            tool: 'search_documents',
            args_hint: { query: 'efficient attention long context' },
            depends_on: [],
          },
          {
            step: 2,
            description: 'Find related sparse-attention work on arXiv',
            tool: 'search_arxiv',
            args_hint: { query: 'sparse attention transformer' },
            depends_on: [1],
          },
          {
            step: 3,
            description: 'Compare complexity and exactness across methods',
            tool: 'synthesize',
            args_hint: {},
            depends_on: [1, 2],
          },
        ],
        toolExecutions: [
          {
            id: 'tool-1',
            tool: 'search_documents',
            label: 'Search documents',
            status: 'done',
            durationMs: 820,
            argsSummary: 'efficient attention long context',
            resultSummary: '2 documents',
          },
        ],
      }}
    />
  </div>
);

export const Thinking = () => (
  <div style={{ maxWidth: 760 }}>
    <ChatBubble
      message={{ id: 'msg-pending', role: 'assistant', content: '', timestamp: T + 2_000 }}
      index={1}
      modelName="GPT-5"
      isStreaming
      thinkingLabel="Retrieving sources"
    />
  </div>
);

export const Streaming = () => (
  <div style={{ maxWidth: 760 }}>
    <ChatBubble
      message={{ id: 'msg-streaming', role: 'assistant', content: '', timestamp: T + 4_000 }}
      index={1}
      modelName="GPT-5"
      isStreaming
      streamingContent={
        'Exact attention can be made fast without approximation. **FlashAttention** tiles the computation so the score matrix never leaves on-chip SRAM, and'
      }
    />
  </div>
);

// The same answer on the dark theme, the live app's default.
export const Dark = () => (
  <DarkSurface>
    <AssistantAnswer />
  </DarkSurface>
);
