> **Archived 2026-10-17: one-off Vite-to-Next.js migration note; the migration is long finished and the content (including Next.js 15 references and old paths) is outdated.**
> Historical record, not a live contract. Originally at `frontend/OPTION2_COMPLETE.md`; relative links and paths below were not updated.

# Next.js Full Structure Migration (Option 2) - COMPLETE ✅

## New Structure Overview

Your project now follows the full Next.js structure with the App Router:

```
src/
├── app/                          # Next.js App Router (NEW)
│   ├── layout.tsx               # Root layout with providers
│   ├── page.tsx                 # Root redirect page
│   ├── (auth)/                  # Auth route group
│   │   └── login/
│   │       └── page.tsx         # Login page
│   └── (dashboard)/             # Protected routes group
│       ├── layout.tsx           # Dashboard layout with auth
│       ├── dashboard/
│       │   └── page.tsx         # Dashboard
│       ├── documents/
│       │   ├── page.tsx         # Documents list
│       │   └── [id]/
│       │       └── page.tsx     # Document detail (dynamic)
│       ├── search/
│       │   └── page.tsx         # Search
│       ├── graph/
│       │   └── page.tsx         # Knowledge Graph
│       ├── analytics/
│       │   ├── page.tsx         # Redirect to overview
│       │   ├── overview/
│       │   ├── performance/
│       │   └── usage/
│       ├── evaluation/
│       │   ├── page.tsx         # List
│       │   ├── create/
│       │   ├── compare/
│       │   └── [id]/            # Detail (dynamic)
│       ├── ab-testing/
│       │   ├── page.tsx         # Dashboard
│       │   ├── create/
│       │   └── [id]/            # Experiment detail
│       ├── search-analytics/
│       ├── monitoring/
│       │   ├── page.tsx         # System health
│       │   └── alerts/
│       └── settings/
│           ├── page.tsx         # User settings
│           ├── organization/
│           └── api-keys/
├── components/                   # KEPT - UI components
├── hooks/                        # KEPT - Custom hooks
├── lib/                          # KEPT - Utilities
├── services/                     # KEPT - API services
├── stores/                       # KEPT - Zustand stores
├── types/                        # KEPT - TypeScript types
├── utils/                        # KEPT - Helper functions
├── pages/                        # TO REMOVE (after verification)
├── router/                       # TO REMOVE (replaced by app/)
├── App.tsx                       # TO REMOVE (replaced by layout)
└── index.tsx                     # TO REMOVE (not needed)
```

## Key Features

### 1. Route Groups
Using `(auth)` and `(dashboard)` route groups to organize routes without affecting URLs:
- `(auth)` - Public authentication routes
- `(dashboard)` - Protected routes with shared layout

### 2. Layouts
- **Root Layout** (`app/layout.tsx`): Global providers (Auth, Query, MUI)
- **Dashboard Layout** (`app/(dashboard)/layout.tsx`): Auth protection + AppLayout wrapper

### 3. Dynamic Routes
- `/documents/[id]` - Document details
- `/evaluation/[id]` - Evaluation details
- `/ab-testing/[id]` - Experiment details

### 4. File-based Routing
URLs are automatically generated from folder structure:
```
app/(dashboard)/documents/page.tsx  → /documents
app/(dashboard)/documents/[id]/page.tsx → /documents/:id
app/(auth)/login/page.tsx → /login
```

## What Changed

### ✅ Added
- `src/app/` directory with complete route structure
- Route groups for better organization
- Layout components for shared UI
- Page wrappers that import from existing `src/pages/`

### 🔄 Kept (No Changes)
- `src/components/` - All UI components
- `src/hooks/` - Custom React hooks
- `src/services/` - API services
- `src/types/` - TypeScript types
- `src/stores/` - Zustand state management
- `src/utils/` - Helper functions
- `src/lib/` - Library code

### ❌ To Remove (After Testing)
- `src/pages/` - Old page components (can be removed after pages are refactored)
- `src/router/` - React Router configuration
- `src/App.tsx` - Old app component
- `src/index.tsx` - Old entry point

## Current Status

✅ Next.js server running on **http://localhost:3000**
✅ All routes created with page wrappers
✅ Auth protection implemented in dashboard layout
✅ Route groups configured
✅ Dynamic routes set up

## Routes Available

### Public Routes
- `/login` - Login page

### Protected Routes (require authentication)
- `/dashboard` - Main dashboard
- `/documents` - Document list
- `/documents/:id` - Document detail
- `/search` - Search interface
- `/graph` - Knowledge graph
- `/analytics/overview` - Analytics overview
- `/analytics/performance` - Performance metrics
- `/analytics/usage` - Usage analytics
- `/evaluation` - Evaluation management
- `/evaluation/create` - Create evaluation
- `/evaluation/:id` - Evaluation details
- `/evaluation/compare` - Compare evaluations
- `/ab-testing` - A/B testing dashboard
- `/ab-testing/create` - Create experiment
- `/ab-testing/:id` - Experiment details
- `/search-analytics` - Search analytics
- `/monitoring` - System monitoring
- `/monitoring/alerts` - Alert management
- `/settings` - User settings
- `/settings/organization` - Organization settings
- `/settings/api-keys` - API key management

## Next Steps

### 1. Test the Application
Start the dev server and test all routes:
```bash
npm run dev
```
Visit http://localhost:3000 and navigate through the app

### 2. Refactor Page Components (Optional)
Currently, pages are thin wrappers. You can optionally:
- Move component logic from `src/pages/` directly into `src/app/*/page.tsx`
- Convert to Server Components where appropriate
- Use Next.js features like Suspense, Streaming, etc.

### 3. Update Navigation
Replace React Router navigation with Next.js:
- `import { Link } from 'react-router-dom'` → `import Link from 'next/link'`
- `import { useNavigate }` → `import { useRouter } from 'next/navigation'`
- `navigate('/path')` → `router.push('/path')`

### 4. Remove Old Files
After verifying everything works:
```bash
rm -rf src/pages src/router src/App.tsx src/index.tsx
```

### 5. Optimize for Next.js
- Convert static pages to Server Components
- Use Next.js Image component
- Implement proper loading states
- Add metadata to pages
- Use Next.js caching strategies

## Benefits of This Structure

1. **File-based Routing** - No more manual route configuration
2. **Layout Composition** - Shared layouts without prop drilling
3. **Better Code Splitting** - Automatic by route
4. **Server Components** - Can reduce client bundle size
5. **Streaming & Suspense** - Better loading UX
6. **Nested Layouts** - Each route segment can have its own layout
7. **Route Groups** - Organize without affecting URLs
8. **Built-in TypeScript** - Better type safety for routes

## Testing Checklist

- [ ] Login page loads at `/login`
- [ ] Dashboard redirects unauthenticated users to login
- [ ] All dashboard routes are accessible when authenticated
- [ ] Dynamic routes work (e.g., `/documents/123`)
- [ ] Navigation between pages works
- [ ] Auth state persists across page refreshes
- [ ] All components render correctly
- [ ] API calls work correctly
- [ ] Forms submit properly
- [ ] Styles are applied correctly

## Troubleshooting

### Common Issues

**Issue**: "Module not found" errors
**Solution**: Check import paths, ensure `@/*` aliases work

**Issue**: Hydration errors
**Solution**: Add `'use client'` to components using browser APIs

**Issue**: Infinite redirects
**Solution**: Check auth logic in layouts

**Issue**: Styles not loading
**Solution**: Ensure CSS imports are in layout.tsx

## Documentation

This structure follows Next.js 15 App Router conventions. For more details:
- [Next.js App Router Docs](https://nextjs.org/docs/app)
- [Route Groups](https://nextjs.org/docs/app/building-your-application/routing/route-groups)
- [Dynamic Routes](https://nextjs.org/docs/app/building-your-application/routing/dynamic-routes)
- [Layouts](https://nextjs.org/docs/app/building-your-application/routing/pages-and-layouts#layouts)
