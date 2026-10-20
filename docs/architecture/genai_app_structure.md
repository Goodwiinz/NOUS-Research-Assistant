# Application Structure v2.0

> **Stale / historical (2026-10-17):** It describes a design from an earlier infrastructure generation (standalone microservices, Qdrant, multiple environments) that is not what runs today. Only `dev` is live and services run inside the single FastAPI backend. Check the code before relying on any statement here. Current contracts: [`docs/engineering/`](../engineering/README.md).

## Improved Architecture for Modern AI Applications

---

## Executive Summary

This architecture improves upon the original RAG system by:

- **Chat-first navigation** - Conversational interfaces as primary entry point
- **Adaptive context awareness** - Dynamic navigation based on user intent and state
- **Conversation threads as content organization** - Instead of siloed pages
- **Unified search-centric paradigm** - Search integrated everywhere, not isolated
- **Multi-modal interaction patterns** - Voice, text, file upload, image understanding
- **Real-time collaboration & sharing** - Essential for modern AI apps
- **Workspace/organization hierarchy** - Supporting team workflows
- **Progressive disclosure** - Show only relevant controls when needed
- **Undo/context preservation** - Keep conversation history accessible
- **Cross-platform continuity** - Seamless experience across devices

---

## Core Architecture Layers

```
┌──────────────────────────────────────────────────────────────────┐
│                      APPLICATION SHELL                           │
│  (Provider Wrapper + Global State + Real-time Listeners)        │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │         PERSISTENT NAVIGATION LAYER                      │   │
│  │  (Workspace Bar + Conversation Sidebar + Quick Actions)  │   │
│  └─────────────────────────────────────────────────────────┘   │
│                          ▼                                       │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │           CONTEXT-AWARE MAIN AREA                       │   │
│  │  (Conversation, Chat, Editor, Tools - Dynamic)          │   │
│  │                                                          │   │
│  │  - Floating Command Palette (⌘K / Ctrl+K)             │   │
│  │  - Adaptive Toolbar (context-specific)                  │   │
│  │  - Inline Actions & Suggestions                         │   │
│  │  - Real-time Collaboration Indicators                   │   │
│  └─────────────────────────────────────────────────────────┘   │
│                          ▼                                       │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │      MODULAR RIGHT SIDEBAR (Collapsible)                │   │
│  │  - Context Panel (active document/search results)       │   │
│  │  - Properties & Settings                                │   │
│  │  - Collaboration & Sharing                              │   │
│  │  - AI Suggestions & Follow-ups                          │   │
│  │  - Citation/Source Inspector                            │   │
│  └─────────────────────────────────────────────────────────┘   │
│                          ▼                                       │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │         COMMAND PALETTE & QUICK ACCESS LAYER            │   │
│  │  (Search, Create, Jump to Conversation, Quick Actions)  │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

---

## Navigation Architecture (Chat-First Model)

### 1. Persistent Workspace Bar (Top)

```
┌──────────────────────────────────────────────────────────────┐
│ [Logo/Home]  [Workspace Picker]  [User/Team]  [⚙️ Settings]  │
├──────────────────────────────────────────────────────────────┤
│
│  Workspace: "AI Research Lab"  ▼
│
│  [👤 User Profile] [Notifications] [Help] [Feedback]
│
└──────────────────────────────────────────────────────────────┘
```

**Key Functions:**

- Switch workspaces/organizations
- Access workspace settings
- Quick user profile access
- Notification center (integrated)
- Help & feedback (contextual)

### 2. Left Sidebar - Conversation Stack (Primary Navigation)

```
┌──────────────────────────────────┐
│  ⌘K  [Search Conversations]      │
├──────────────────────────────────┤
│                                  │
│  [+ New Chat]  [Filters ▼]       │
│                                  │
│  ┌────────────────────────────┐  │
│  │ TODAY                      │  │
│  ├────────────────────────────┤  │
│  │ 📌 Pinned                  │  │
│  │ • Research Paper Summary   │  │
│  │ • RAG Optimization Guide   │  │
│  │                            │  │
│  │ ⏱️  Recent                 │  │
│  │ • "How does BIT improve..." │  │
│  │ • "Compare BAAI/bge vs..." │  │
│  │ • "Fine-tune embeddings..."│  │
│  │                            │  │
│  │ YESTERDAY                  │  │
│  │ • "Prompt injection defense"│  │
│  │ • "Evaluate RAG metrics"   │  │
│  │                            │  │
│  │ LAST 7 DAYS                │  │
│  │ • [Archive of conversations]│  │
│  │                            │  │
│  │ COLLECTIONS                │  │
│  │ 📁 ML Research             │  │
│  │ 📁 Project: BIT Defense    │  │
│  │ 📁 Paper Notes             │  │
│  │                            │  │
│  │ SHARED WITH ME             │  │
│  │ 👥 "LLM Security Brief"    │  │
│  │    - shared by @alex       │  │
│  │                            │  │
│  │ WORKSPACES                 │  │
│  │ 🔗 ML Lab (Active)         │  │
│  │ 🔗 Academic Projects       │  │
│  │ 🔗 Personal Experiments    │  │
│  └────────────────────────────┘  │
│                                  │
└──────────────────────────────────┘
```

**Features:**

- **Smart Search**: Full-text search across conversation history + content
- **Grouping**: Auto-organized by time, but customizable by collections
- **Pinning**: Keep important conversations at top
- **Collections/Folders**: Organize conversations by project/topic
- **Shared Access**: See conversations shared by team members
- **Workspace Switching**: Quick access to other workspaces within same org

### 3. Main Content Area (Adaptive)

#### A. Chat View (Primary)

```
┌─────────────────────────────────────────────────────────────┐
│  📄 Research Paper Analysis  ↑↓  [Share] [More ...]        │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  Assistant (9:42 AM)                                        │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ Based on your paper, the key findings are:           │  │
│  │                                                      │  │
│  │ 1. **BIT improves precision** by 15% on evaluation  │  │
│  │ 2. **Embedding fine-tuning** reduces hallucinations │  │
│  │ 3. **Defense strategy** against prompt injection    │  │
│  │                                                      │  │
│  │ Would you like me to:                               │  │
│  │ [Summarize methodology] [Compare with other works] │  │
│  │ [Extract citations] [Generate research questions]  │  │
│  └──────────────────────────────────────────────────────┘  │
│                              👍  👎  📋 Copy  🔄 Regenerate │
│                                                              │
│  You (9:44 AM)                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ Can you compare this with the DistilBERT approach?  │  │
│  │ [Attached: paper-2.pdf] [Attached: notes.md]        │  │
│  └──────────────────────────────────────────────────────┘  │
│                                 📎 Attachments  🎤 Voice    │
│                                                              │
│  ┌─ INPUT AREA ─────────────────────────────────────────┐  │
│  │ Type message, paste files, record voice, or ⌘K...  │  │
│  │                                                      │  │
│  │ [📎 Attach]  [🎤 Voice]  [➕ Add]    [📤 Send] ✨ │  │
│  └─────────────────────────────────────────────────────┘  │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

#### B. Context-Aware Toolbar

```
Appears dynamically based on:
- Current document type
- Active conversation state
- Available AI models
- Workspace capabilities

Examples:
┌────────────────────────────────────────┐
│ [📋 Extract] [🔄 Regenerate] [🔗 Link] │  (Chat context)
│ [📊 Analyze] [🎨 Visualize] [💾 Export]│
└────────────────────────────────────────┘
```

### 4. Right Sidebar - Context Panel (Collapsible)

```
┌──────────────────────────────────────────┐
│  [X] Context & Actions                   │
├──────────────────────────────────────────┤
│                                          │
│  📄 DOCUMENT INSPECTOR                   │
│  ├─ File: research-paper-2024.pdf        │
│  ├─ Pages: 1-5 (Loaded)                  │
│  ├─ Tokens: 8,243                        │
│  ├─ Embedding: bge-large-en-v1.5        │
│  ├─ Last updated: 2 hours ago            │
│  └─ [🔗 View Full Document]              │
│                                          │
│  🔍 SEARCH RESULTS (Related)              │
│  ├─ "RAG Optimization" (85% match)       │
│  │  📄 arxiv-2024-05-001.pdf             │
│  │  Page 3: "Our approach improves..."   │
│  │  [Preview]  [Load]                   │
│  │                                      │
│  └─ "Embedding Fine-tuning" (72% match) │
│     📄 internal-research-notes.md        │
│                                          │
│  👥 COLLABORATORS                        │
│  ├─ @alice (viewing)                     │
│  ├─ @bob (edited 10 min ago)             │
│  └─ @charlie (can view)                  │
│                                          │
│  💬 FOLLOW-UP SUGGESTIONS                │
│  ├─ "Compare with Transformer models"    │
│  ├─ "Evaluate on benchmark datasets"     │
│  ├─ "Generate test cases for defense"    │
│  └─ [Show All]                           │
│                                          │
│  ⚙️  PROPERTIES                          │
│  ├─ Model: Claude 3.5 Sonnet             │
│  ├─ Temperature: 0.7                     │
│  ├─ Max tokens: 2000                     │
│  └─ [Advanced Settings]                  │
│                                          │
│  🔗 CITATIONS & SOURCES                  │
│  ├─ [1] cited-paper-2024.pdf             │
│  ├─ [2] research-notes.md                │
│  ├─ [3] internal-wiki-page               │
│  └─ [Citation Format ▼]                  │
│                                          │
└──────────────────────────────────────────┘
```

**Panels:**

- Document Inspector (metadata, tokens, embeddings)
- Related Search Results (context-aware)
- Active Collaborators
- Follow-up Suggestions
- Model Properties
- Citations & Source Inspector

### 5. Command Palette (Floating)

Accessible via **⌘K** / **Ctrl+K** everywhere:

```
┌──────────────────────────────────────────────────┐
│  ⌘K  [Search or command...]                      │
├──────────────────────────────────────────────────┤
│                                                  │
│  ⚡ QUICK ACTIONS                               │
│  • ➕ New Chat                                  │
│  • 📤 Upload Document                          │
│  • 🔗 Create Collection                        │
│  • 👥 Invite Team Member                       │
│  • 📊 Go to Dashboard                          │
│  • ⚙️  Open Settings                           │
│                                                  │
│  🔍 RECENT CONVERSATIONS                        │
│  • "How does BIT improve RAG precision?"       │
│  • "Fine-tune BAAI/bge embeddings"             │
│  • "Prompt injection defense strategies"       │
│                                                  │
│  📁 COLLECTIONS                                 │
│  • ML Research                                  │
│  • Project: BIT Defense                         │
│  • Paper Notes                                  │
│                                                  │
│  🤖 AI MODELS                                   │
│  • Claude 3.5 Sonnet (default)                 │
│  • GPT-4o                                       │
│  • Local Llama 2                                │
│                                                  │
│  👤 PEOPLE                                      │
│  • @alice                                       │
│  • @bob                                         │
│  • @charlie                                     │
│                                                  │
└──────────────────────────────────────────────────┘
```

---

## Page Routes & Organization

### 1. Authentication Layer (No Sidebar)

| Route                  | Component          | Purpose                         |
| ---------------------- | ------------------ | ------------------------------- |
| `/auth/login`          | Login              | Email + password authentication |
| `/auth/register`       | Register           | Account creation                |
| `/auth/reset-password` | Password Reset     | Recovery flow                   |
| `/auth/verify-email`   | Email Verification | Email confirmation              |
| `/auth/sso-callback`   | SSO Handler        | OAuth/SAML callback             |

### 2. Workspace/Onboarding Layer (Minimal Sidebar)

| Route                  | Component          | Purpose                     |
| ---------------------- | ------------------ | --------------------------- |
| `/onboarding`          | Onboarding         | Initial setup wizard        |
| `/workspaces`          | Workspace Selector | Multi-workspace selection   |
| `/workspace/:id/setup` | Workspace Setup    | First-time workspace config |

### 3. Main Application (Full Navigation)

#### A. Chat & Conversation Routes

| Route                | Component       | Purpose                    |
| -------------------- | --------------- | -------------------------- |
| `/chat`              | Chat Hub        | List view + chat selector  |
| `/chat/:id`          | Active Chat     | Main chat interface        |
| `/chat/:id/details`  | Chat Metadata   | View conversation metadata |
| `/threads/:threadId` | Specific Thread | Deep link to thread        |

#### B. Documents & Knowledge Base

| Route                       | Component          | Purpose                         |
| --------------------------- | ------------------ | ------------------------------- |
| `/documents`                | Document Hub       | Browse all documents            |
| `/documents/upload`         | Upload Interface   | Upload new documents            |
| `/documents/search`         | Document Search    | Full-text search                |
| `/documents/:id`            | Document Viewer    | View single document + chat     |
| `/documents/:id/embeddings` | Embedding Viewer   | View embedding metadata         |
| `/collections`              | Collections        | Browse document collections     |
| `/collections/:id`          | Collection Details | View collection contents        |
| `/arxiv`                    | ArXiv Ingestion    | Browse + ingest research papers |

#### C. Workspace Tools

| Route                       | Component      | Purpose                         |
| --------------------------- | -------------- | ------------------------------- |
| `/tools/research-assistant` | Research Tool  | Multi-doc research interface    |
| `/tools/code-analyzer`      | Code Analysis  | Code review + suggestions       |
| `/tools/embeddings-lab`     | Embeddings Lab | Fine-tune + evaluate embeddings |
| `/tools/rag-evaluator`      | RAG Evaluator  | Evaluate RAG performance        |
| `/tools/prompt-tester`      | Prompt Lab     | Test prompts against models     |

#### D. Analytics & Insights

| Route                    | Component         | Purpose                           |
| ------------------------ | ----------------- | --------------------------------- |
| `/dashboard`             | Main Dashboard    | System overview + metrics         |
| `/analytics/usage`       | Usage Analytics   | Token usage, conversations, docs  |
| `/analytics/models`      | Model Performance | Model comparison, latency metrics |
| `/analytics/rag-metrics` | RAG Metrics       | Precision, MRR, retrieval quality |
| `/analytics/realtime`    | Real-time Monitor | Live processing status            |

#### E. Configuration & Admin

| Route                    | Component          | Purpose                       |
| ------------------------ | ------------------ | ----------------------------- |
| `/settings/profile`      | User Profile       | Personal settings             |
| `/settings/preferences`  | Preferences        | UI/UX preferences             |
| `/settings/api-keys`     | API Keys           | Manage API keys + webhooks    |
| `/settings/integrations` | Integrations       | External service connections  |
| `/workspace/settings`    | Workspace Settings | Team workspace config         |
| `/workspace/members`     | Team Members       | Manage team access            |
| `/workspace/billing`     | Billing            | Subscription + usage tracking |

#### F. Specialized Pages

| Route              | Component        | Purpose                          |
| ------------------ | ---------------- | -------------------------------- |
| `/entities`        | Entity Extractor | Extract + manage knowledge graph |
| `/security-demo`   | Security Demo    | Test LLM security                |
| `/evaluation-demo` | Evaluation Demo  | RAG evaluation walkthrough       |
| `/help`            | Help Center      | Documentation + FAQs             |

---

## State Management & Context Layers

### 1. Global Application State

```typescript
AppContext {
  // User & Auth
  user: User
  workspace: Workspace
  permissions: PermissionSet

  // Conversation State
  activeConversation: Conversation | null
  conversations: Conversation[]
  conversationCache: Map<string, Conversation>

  // Document State
  documents: Document[]
  documentCache: Map<string, Document>
  uploadQueue: Upload[]

  // UI State
  sidebarOpen: boolean
  rightPanelOpen: boolean
  rightPanelContent: 'context' | 'properties' | 'citations'
  mobileViewport: boolean

  // Real-time
  activeUsers: User[]
  socketConnected: boolean
  pendingSyncActions: Action[]
}
```

### 2. Conversation Context

```typescript
ConversationContext {
  conversationId: string
  messages: Message[]
  pendingMessage: string | null
  isLoading: boolean
  selectedModel: Model
  modelSettings: ModelConfig
  attachments: Attachment[]
  threadStack: Thread[] // For threaded conversations
  undo/redo: HistoryStack
}
```

### 3. Document Context

```typescript
DocumentContext {
  activeDocument: Document | null
  relatedDocuments: Document[]
  embeddings: EmbeddingMetadata
  searchQuery: string
  searchResults: SearchResult[]
  selectedText: Selection | null
  annotations: Annotation[]
}
```

---

## Component Hierarchy

```
App (Root with Providers)
├── Auth Guard
│   ├── /auth/* (Auth Pages - No Layout)
│   │   ├── Login
│   │   ├── Register
│   │   └── PasswordReset
│   │
│   └── Authenticated Routes (With Layout)
│       ├── RootLayout
│       │   ├── WorkspaceBar (Top Navigation)
│       │   ├── MainContainer
│       │   │   ├── LeftSidebar (Conversation Stack)
│       │   │   ├── MainContent (Adaptive)
│       │   │   │   ├── /chat/:id → ChatView
│       │   │   │   ├── /documents/:id → DocumentViewer + Chat
│       │   │   │   ├── /tools/* → Tool Interface
│       │   │   │   ├── /dashboard → Analytics Dashboard
│       │   │   │   ├── /settings/* → Settings Panel
│       │   │   │   └── ...other routes
│       │   │   │
│       │   │   └── RightSidebar (Context Panel)
│       │   │       ├── DocumentInspector
│       │   │       ├── SearchResults
│       │   │       ├── Collaborators
│       │   │       ├── FollowupSuggestions
│       │   │       └── CitationInspector
│       │   │
│       │   ├── CommandPalette (Modal)
│       │   └── NotificationCenter (Toast/Panel)
│       │
│       └── /workspace/setup (Workspace Setup)
```

---

## Key Improvements Over Original Structure

### 1. **Chat-First Navigation**

- ❌ Old: Siloed pages with separate chat section
- ✅ New: Conversations as primary content container

### 2. **Unified Search**

- ❌ Old: Search isolated in `/search` route
- ✅ New: Global search via command palette, accessible everywhere

### 3. **Context Awareness**

- ❌ Old: Static sidebars and layouts
- ✅ New: Adaptive UI that responds to conversation state

### 4. **Document-to-Chat Integration**

- ❌ Old: Upload → separate document view → separate chat
- ✅ New: Unified view with documents + chat in same context

### 5. **Real-time Collaboration**

- ❌ Old: No collaboration features
- ✅ New: Show active collaborators, shared conversations, team workspaces

### 6. **Progressive Disclosure**

- ❌ Old: All toolbar options always visible
- ✅ New: Context-specific toolbar, collapsible panels

### 7. **Breadcrumb Breadth**

- ❌ Old: Breadcrumbs on all pages
- ✅ New: Smart breadcrumbs + conversation title as navigation anchor

### 8. **Conversation Threading**

- ❌ Old: Flat conversation structure
- ✅ New: Support for conversation threads and branches

### 9. **Workspace Hierarchy**

- ❌ Old: Single workspace model
- ✅ New: Multi-workspace support with team organization

### 10. **Undo/History**

- ❌ Old: No undo capability
- ✅ New: Full conversation history with undo/redo support

---

## User Flows

### Research Document Workflow

```
User Start
  ├─ ⌘K Search for "RAG precision evaluation"
  │   └─ Select paper from results
  │       ├─ Opens document viewer with chat context
  │       ├─ Right panel shows embeddings + related papers
  │       └─ Chat input: "Summarize methodology"
  │           └─ Response appears with citations
  │               └─ User clicks citation → document loaded
  │
  └─ Or: Upload new document
      ├─ Drag + drop to main area
      ├─ Processing with real-time status
      ├─ Navigate to /documents/:id
      ├─ Start asking questions
      └─ System suggests follow-ups
```

### Team Collaboration Flow

```
User A creates research chat
  ├─ [Share] button → invite team
  │   └─ Sends link to @alice, @bob
  │
User B joins conversation
  ├─ Sees conversation in "Shared with Me"
  ├─ Right panel shows active collaborators
  ├─ Can add follow-up messages
  └─ Changes sync in real-time
      └─ User A sees "Bob is typing..."

User C reviews conversation
  ├─ Accesses via workspace
  ├─ Can export conversation
  ├─ Can create collection from thread
  └─ Can share individual insights
```

### Analytics Workflow

```
Manager accesses /dashboard
  ├─ Overview cards show team metrics
  ├─ Clicks on "Model Usage" → /analytics/models
  ├─ Sees cost breakdown by model
  ├─ Filters by date range
  └─ Exports report
      └─ Can drill into individual conversations
```

---

## Mobile Responsive Behavior

### Mobile (< 640px)

```
┌─────────────────────┐
│ [Menu] [Workspace]  │  ← Workspace bar (compact)
├─────────────────────┤
│                     │
│  Main Content Area  │  ← Full width, no sidebars
│  (Chat View)        │
│                     │
│                     │
│                     │
│  ⌘K                 │  ← Command palette still accessible
│  [📎] [🎤] [Send]   │
│                     │
└─────────────────────┘

Sidebar: Drawer from left (slide-in)
Right Panel: Drawer from right (slide-in, OR bottom sheet)
```

### Tablet (640px - 1024px)

```
┌──────────────────────────────────────┐
│ [Logo] [Workspace]    [Settings]     │
├──────────────────────────────────────┤
│             │                        │
│  Sidebar    │  Main Content Area     │
│ (Narrow)    │  (Chat View)           │
│             │                        │
│             │  ⌘K                    │
│             │  [📎] [🎤] [Send]      │
│             │                        │
└──────────────────────────────────────┘

Right Panel: Hidden by default, accessible via ← → chevron
```

### Desktop (> 1024px)

```
Full layout with all three columns visible
(as shown in architecture diagrams above)
```

---

## Design System Integration

### Color Tokens for GenAI Apps

```css
/* Primary States */
--color-assistant: hsl(200, 70%, 50%)    /* AI responses */
--color-user: hsl(120, 60%, 40%)         /* User messages */
--color-system: hsl(0, 0%, 60%)          /* System messages */
--color-error: hsl(0, 100%, 50%)         /* Errors/warnings */
--color-success: hsl(120, 100%, 35%)     /* Success states */

/* Semantic */
--color-thinking: hsl(280, 60%, 50%)     /* Processing/thinking */
--color-loading: hsl(200, 80%, 45%)      /* Loading states */
--color-attention: hsl(45, 100%, 50%)    /* Attention needed */

/* Interaction Layers */
--shadow-hover: 0 8px 16px rgba(0,0,0,0.08)
--shadow-active: 0 4px 8px rgba(0,0,0,0.12)
--transition-interactive: 150ms cubic-bezier(0.16, 1, 0.3, 1)
```

### Typography System

```css
/* Chat Messages */
--font-size-message: 14px
--line-height-message: 1.5
--font-weight-assistant-bold: 600

/* UI Controls */
--font-size-label: 12px
--font-size-button: 14px
--font-size-heading: 18px

/* Code */
--font-family-mono: 'Fira Code', monospace
--font-size-code: 13px
--background-code: hsl(0, 0%, 8%)
```

---

## Real-Time Features Architecture

### WebSocket Events

```typescript
// Message Events
socket.on("message:new", (message) => {});
socket.on("message:updated", (message) => {});
socket.on("message:deleted", (id) => {});

// Collaboration Events
socket.on("user:joined", (user) => {});
socket.on("user:left", (user) => {});
socket.on("user:typing", (userId) => {});

// Document Events
socket.on("document:uploaded", (document) => {});
socket.on("document:processed", (document) => {});
socket.on("embedding:generated", (embedding) => {});

// System Events
socket.on("processing:started", (jobId) => {});
socket.on("processing:progress", (progress) => {});
socket.on("processing:completed", (result) => {});
```

---

## Summary Table: Route Organization

| Feature                  | Routes                                   | Access                    |
| ------------------------ | ---------------------------------------- | ------------------------- |
| **Chat & Conversations** | `/chat`, `/chat/:id`                     | Sidebar + Command K       |
| **Documents**            | `/documents`, `/documents/:id`, `/arxiv` | Sidebar + Upload          |
| **Tools**                | `/tools/*`                               | Workspace Menu            |
| **Analytics**            | `/dashboard`, `/analytics/*`             | Workspace Menu            |
| **Settings**             | `/settings/*`, `/workspace/*`            | Profile + Workspace Menu  |
| **Collections**          | `/collections`, `/collections/:id`       | Sidebar                   |
| **Auth**                 | `/auth/*`                                | Direct access (no layout) |

---

## Migration Path from Original

### Phase 1: Foundation (Week 1-2)

- Implement new sidebar structure
- Create command palette component
- Add right panel framework

### Phase 2: Chat Integration (Week 3-4)

- Refactor pages to chat-first model
- Move document upload to main flow
- Add real-time sync

### Phase 3: Collaboration (Week 5-6)

- Add sharing/permissions
- Implement collaborative indicators
- Add workspace management

### Phase 4: Polish (Week 7-8)

- Responsive design across breakpoints
- Animation & transitions
- Performance optimization

---

## Best Practices Implemented

✅ **Predictive Navigation** - Suggest next actions based on context
✅ **Gesture-Friendly** - Design for touch + keyboard
✅ **Accessibility First** - WCAG 2.1 AA compliance
✅ **Mobile-First** - Design responsive from smallest screen
✅ **Progressive Disclosure** - Show only relevant controls
✅ **Visual Hierarchy** - Clear information priority
✅ **Undo/Redo** - Full conversation history support
✅ **Search-Centric** - Global search as primary discovery
✅ **Real-Time** - Live collaboration + sync
✅ **Context Awareness** - Adaptive UI based on state
