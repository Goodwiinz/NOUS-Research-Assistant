> **Archived 2026-10-17: one-off Vite-to-Next.js migration note; the migration is long finished and the content (including Next.js 15 references and old paths) is outdated.**
> Historical record, not a live contract. Originally at `frontend/NEXTJS_MIGRATION_GUIDE.md`; relative links and paths below were not updated.

# Next.js Migration Guide

## Overview
This project has been migrated from Vite to Next.js 15 with the App Router.

## Key Changes

### 1. Build System
- **Before**: Vite
- **After**: Next.js with built-in webpack/turbopack
- **Configuration**: `next.config.js` replaces `vite.config.ts`

### 2. Routing
- **Before**: React Router DOM with `createBrowserRouter`
- **After**: Next.js App Router (file-system based routing)
- **Migration Status**: Needs migration from `src/router/index.tsx`

### 3. Entry Point
- **Before**: `index.html` + `src/index.tsx`
- **After**: `app/layout.tsx` + `app/page.tsx`
- **Status**: Created

### 4. Dependencies Removed
- `vite`
- `@vitejs/plugin-react`
- `vitest` (replaced with jest)
- `react-router-dom` (to be removed - using Next.js routing)

### 5. Dependencies Added
- `next` - Next.js framework
- `@mui/material-nextjs` - MUI integration for Next.js
- `eslint-config-next` - Next.js ESLint configuration

## Migration Steps Needed

### Routing Migration
The current application uses React Router with the following routes:

1. **Authentication Routes**:
   - `/login` - LoginPage

2. **Protected Routes** (wrapped in AppLayout):
   - `/` - DashboardPage
   - `/documents` - DocumentsPage
   - `/documents/:id` - DocumentDetailPage
   - `/search` - SearchPage
   - `/graph` - KnowledgeGraphPage
   - `/analytics` - Analytics routes
   - `/evaluation` - Evaluation routes
   - `/ab-testing` - A/B Testing routes
   - `/monitoring` - Monitoring routes
   - `/settings` - Settings routes

### Next.js App Router Structure
To migrate, create the following structure:

```
app/
├── layout.tsx (root layout - DONE)
├── page.tsx (root redirect - DONE)
├── login/
│   └── page.tsx
├── dashboard/
│   └── page.tsx
├── documents/
│   ├── page.tsx
│   └── [id]/
│       └── page.tsx
├── search/
│   └── page.tsx
├── graph/
│   └── page.tsx
├── analytics/
│   ├── overview/
│   │   └── page.tsx
│   ├── performance/
│   │   └── page.tsx
│   └── usage/
│       └── page.tsx
... (and so on)
```

### Code Changes Required

1. **Remove React Router imports**:
   - Replace `import { useRouter } from 'react-router-dom'` with `import { useRouter } from 'next/navigation'`
   - Replace `import { useNavigate }` with `useRouter().push()`
   - Replace `import { Link }` with `import Link from 'next/link'`
   - Replace `import { useParams }` with `import { useParams } from 'next/navigation'`

2. **Client Components**:
   - Add `'use client'` directive to components that use hooks or browser APIs
   - Server components by default don't need the directive

3. **Image Optimization**:
   - Replace `<img>` with Next.js `<Image>` component for optimized images
   - Configure image domains in `next.config.js`

4. **API Routes** (if needed):
   - Create `app/api/` directory for API routes
   - Use Next.js API Routes instead of separate backend calls (optional)

5. **Environment Variables**:
   - Prefix with `NEXT_PUBLIC_` for client-side access
   - Update `.env.local` accordingly

## Running the Application

### Development
```bash
npm run dev
```
Runs on http://localhost:3000 by default

### Production Build
```bash
npm run build
npm start
```

### Testing
```bash
npm test          # Run Jest tests
npm run test:e2e  # Run Playwright E2E tests
```

## Notes

- Next.js uses file-system based routing - each folder in `app/` represents a route segment
- Layout components are shared across routes in the same folder
- Server Components are the default - use 'use client' for client-side interactivity
- API proxy is configured in `next.config.js` to forward `/api/*` to `http://localhost:8000/api/*`

## TODO

- [ ] Migrate all routes from React Router to Next.js App Router
- [ ] Update all navigation components to use Next.js Link
- [ ] Test all pages and ensure they work correctly
- [ ] Update tests for Next.js environment
- [ ] Remove react-router-dom dependency after migration complete
