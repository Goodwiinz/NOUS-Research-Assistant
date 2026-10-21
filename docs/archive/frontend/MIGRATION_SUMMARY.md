> **Archived 2026-10-17: one-off Vite-to-Next.js migration note; the migration is long finished and the content (including Next.js 15 references and old paths) is outdated.**
> Historical record, not a live contract. Originally at `frontend/MIGRATION_SUMMARY.md`; relative links and paths below were not updated.

# Vite to Next.js Migration - Complete ✓

## Summary

Your RAG system frontend has been successfully migrated from Vite to Next.js 15 with the App Router.

## What Was Changed

### 1. Dependencies
**Removed:**
- `vite` - Build tool
- `@vitejs/plugin-react` - Vite React plugin
- `vitest` - Testing framework (replaced with Jest)
- Removed `type: "module"` from package.json

**Added:**
- `next` (v15.1.3) - Next.js framework
- `@mui/material-nextjs` - MUI integration for Next.js
- `eslint-config-next` - Next.js ESLint configuration

### 2. Configuration Files
**Created:**
- `next.config.js` - Next.js configuration with MUI support and API proxy
- `.eslintrc.json` - Next.js ESLint configuration
- `app/layout.tsx` - Root layout with providers (Auth, Query, MUI)
- `app/page.tsx` - Root page with auth-based redirects

**Updated:**
- `tsconfig.json` - Updated for Next.js compatibility
- `package.json` - Updated scripts and dependencies

**Removed:**
- `index.html` - No longer needed (Next.js handles this)

### 3. Scripts Updated
```json
{
  "dev": "next dev",           // Start dev server (http://localhost:3000)
  "build": "next build",       // Build for production
  "start": "next start",       // Start production server
  "lint": "next lint"          // Run Next.js linter
}
```

## Current Status

✅ Next.js is installed and configured
✅ TypeScript configuration updated
✅ MUI integration set up
✅ ESLint configured for Next.js
✅ Root layout and page created
✅ Server successfully starts on http://localhost:3001

## Next Steps

### Important: Route Migration Required

Your application currently uses React Router with extensive routing. You need to migrate these routes to Next.js App Router:

**Current React Router structure** (`src/router/index.tsx`):
- `/login` → Login page
- `/` → Dashboard (protected)
- `/documents` → Documents list
- `/documents/:id` → Document detail
- `/search` → Search page
- `/graph` → Knowledge graph
- And many more routes for analytics, evaluation, monitoring, settings, etc.

**Migration approach:**

1. **Manual Migration** (Recommended for learning Next.js):
   - Create route folders in `app/` directory
   - Copy components from `src/pages/` to corresponding `app/*/page.tsx` files
   - Add `'use client'` directive to components using hooks/browser APIs
   - Update navigation from `react-router-dom` to `next/navigation`

2. **Keep React Router** (Quick workaround):
   - You could temporarily keep React Router by wrapping it in a client component
   - But this defeats the purpose of Next.js SSR and routing benefits

### Specific Files That Need Updates

1. **Navigation components** - Replace:
   - `import { Link } from 'react-router-dom'` → `import Link from 'next/link'`
   - `import { useNavigate }` → `import { useRouter } from 'next/navigation'`
   - `navigate('/path')` → `router.push('/path')`

2. **Page components** - Move from:
   - `src/pages/*Page.tsx` → `app/*/page.tsx`
   - Add `'use client'` if they use React hooks

3. **Layout components**:
   - `src/components/layout/AppLayout.tsx` → Consider using as Next.js layout
   - Shared layouts go in `app/layout.tsx` or nested `app/*/layout.tsx`

## Testing

Start the development server:
```bash
cd /Users/goodwiinz/development/RAG_system/rag/frontend
npm run dev
```

The server will run on http://localhost:3001 (or 3000 if available)

## Configuration Details

### API Proxy
The `next.config.js` includes a rewrite rule to proxy API requests:
- Frontend: `http://localhost:3001/api/*`
- Backend: `http://localhost:8000/api/*`

### Image Optimization
Configured to allow images from `http://localhost:8000`

### MUI Integration
Using `@mui/material-nextjs` for proper SSR support with emotion cache

## Documentation

See `NEXTJS_MIGRATION_GUIDE.md` for detailed migration instructions and Next.js routing patterns.

## Notes

- The existing `src/` directory structure remains intact
- All components, hooks, services, etc. are still in `src/`
- Only the entry point and routing system have changed
- You can migrate routes incrementally - start with login and dashboard
