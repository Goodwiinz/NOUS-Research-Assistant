> **Archived 2026-10-17: one-off Vite-to-Next.js migration note; the migration is long finished and the content (including Next.js 15 references and old paths) is outdated.**
> Historical record, not a live contract. Originally at `frontend/QUICK_START.md`; relative links and paths below were not updated.

# Quick Start Guide - Next.js Migration Complete

## What Just Happened?

Your RAG system frontend has been fully migrated to Next.js with the App Router using **Option 2** - the full Next.js structure.

## Current State

✅ **Vite completely removed**
✅ **Next.js 15 with App Router configured**
✅ **All 21 routes created and organized**
✅ **Route groups implemented for better organization**
✅ **Auth protection setup in dashboard layout**
✅ **Server tested and working**

## Start Developing

```bash
cd /Users/goodwiinz/development/RAG_system/rag/frontend
npm run dev
```

Visit: **http://localhost:3000**

## Quick Reference

### Available Routes

**Public:**
- `/login` - Login page

**Protected (Dashboard):**
- `/dashboard` - Main dashboard
- `/documents` - Documents & `/documents/:id`
- `/search` - Search interface
- `/graph` - Knowledge graph
- `/analytics/*` - Overview, Performance, Usage
- `/evaluation/*` - Management, Create, Details, Compare
- `/ab-testing/*` - Dashboard, Create, Details
- `/search-analytics` - Search analytics
- `/monitoring/*` - System, Alerts
- `/settings/*` - User, Organization, API Keys

### Project Structure

```
src/
├── app/              ← Next.js routes (NEW!)
│   ├── (auth)/      ← Public routes
│   └── (dashboard)/ ← Protected routes
├── components/      ← Your UI components (UNCHANGED)
├── hooks/           ← Custom hooks (UNCHANGED)
├── services/        ← API services (UNCHANGED)
├── types/           ← TypeScript types (UNCHANGED)
└── ...              ← Everything else stays the same
```

### Key Files

- **Root Layout**: `src/app/layout.tsx`
- **Dashboard Layout**: `src/app/(dashboard)/layout.tsx`
- **Next Config**: `next.config.js`
- **TypeScript Config**: `tsconfig.json`

## What's Different?

### Before (Vite + React Router)
```typescript
// src/router/index.tsx
const router = createBrowserRouter([...])
```

### After (Next.js App Router)
```
src/app/
  (dashboard)/
    documents/
      page.tsx  ← Automatically becomes /documents
```

## Next Steps

### 1. Test Everything
- Navigate through all pages
- Test authentication flow
- Verify API calls work
- Check responsive design

### 2. Optional Refactoring
Currently, pages are wrappers around old components:
```typescript
// src/app/(dashboard)/documents/page.tsx
'use client';
import DocumentsPage from '@/pages/documents/DocumentsPage';
export default function Documents() {
  return <DocumentsPage />;
}
```

You can optionally:
- Move logic from `src/pages/*` into `src/app/*`
- Use Server Components where possible
- Leverage Next.js features (Image, Link, etc.)

### 3. Clean Up Old Files
After testing, remove:
```bash
rm -rf src/pages src/router src/App.tsx src/index.tsx
```

## Common Tasks

### Add a New Page
```bash
# Create route: /my-new-page
mkdir -p src/app/\(dashboard\)/my-new-page
cat > src/app/\(dashboard\)/my-new-page/page.tsx << 'EOL'
'use client';

export default function MyNewPage() {
  return <div>My New Page</div>;
}
EOL
```

### Navigation
Replace React Router imports:
```typescript
// Before
import { Link, useNavigate } from 'react-router-dom';
const navigate = useNavigate();
navigate('/path');

// After
import Link from 'next/link';
import { useRouter } from 'next/navigation';
const router = useRouter();
router.push('/path');
```

### Add Middleware (Auth, etc.)
Create `src/middleware.ts`:
```typescript
import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

export function middleware(request: NextRequest) {
  // Your middleware logic
  return NextResponse.next();
}

export const config = {
  matcher: ['/dashboard/:path*'],
};
```

## Documentation

📚 **Project Docs:**
- [OPTION2_COMPLETE.md](OPTION2_COMPLETE.md) - Full migration details
- [NEXTJS_MIGRATION_GUIDE.md](NEXTJS_MIGRATION_GUIDE.md) - Migration guide
- [MIGRATION_SUMMARY.md](MIGRATION_SUMMARY.md) - Quick overview

📚 **Next.js Docs:**
- [App Router](https://nextjs.org/docs/app)
- [Routing](https://nextjs.org/docs/app/building-your-application/routing)
- [Data Fetching](https://nextjs.org/docs/app/building-your-application/data-fetching)

## Troubleshooting

**"Module not found" errors?**
→ Check import paths use `@/*` alias

**Hydration errors?**
→ Add `'use client'` to components using browser APIs

**Auth not working?**
→ Check `src/app/(dashboard)/layout.tsx` auth logic

**Styles broken?**
→ Ensure CSS imports in `src/app/layout.tsx`

## Get Help

Issues? Check:
1. Console for errors
2. Next.js dev server output
3. Browser network tab
4. Documentation files above

---

**You're all set! 🚀 Your Next.js migration is complete.**
