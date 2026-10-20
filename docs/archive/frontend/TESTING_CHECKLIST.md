> **Archived 2026-10-17: one-off post-migration smoke checklist; superseded by the Vitest and Playwright suites.**
> Historical record, not a live contract. Originally at `frontend/TESTING_CHECKLIST.md`; relative links and paths below were not updated.

# Testing Checklist - Next.js Migration

Use this checklist to verify your Next.js migration is working correctly.

## ✅ Server Startup

- [x] Server starts without errors
- [x] Server runs on port 3000 (or available port)
- [ ] No TypeScript errors in console
- [ ] No ESLint errors

## ✅ Routing & Navigation

### Public Routes
- [ ] `/login` loads correctly
- [ ] Login form is visible and functional

### Protected Routes
- [ ] Unauthenticated users redirect to `/login`
- [ ] `/dashboard` loads after authentication
- [ ] All dashboard routes are accessible when authenticated

### Document Routes
- [ ] `/documents` shows document list
- [ ] `/documents/:id` shows document details
- [ ] Dynamic routing works for different document IDs

### Search & Graph
- [ ] `/search` loads search interface
- [ ] `/graph` displays knowledge graph

### Analytics Routes
- [ ] `/analytics` redirects to `/analytics/overview`
- [ ] `/analytics/overview` loads
- [ ] `/analytics/performance` loads
- [ ] `/analytics/usage` loads

### Evaluation Routes
- [ ] `/evaluation` shows evaluation list
- [ ] `/evaluation/create` loads creation form
- [ ] `/evaluation/:id` shows evaluation details
- [ ] `/evaluation/compare` loads comparison view

### A/B Testing Routes
- [ ] `/ab-testing` shows dashboard
- [ ] `/ab-testing/create` loads experiment form
- [ ] `/ab-testing/:id` shows experiment details

### Monitoring & Settings
- [ ] `/monitoring` loads system monitoring
- [ ] `/monitoring/alerts` shows alerts
- [ ] `/settings` loads user settings
- [ ] `/settings/organization` loads org settings
- [ ] `/settings/api-keys` shows API key management

## ✅ Authentication

- [ ] Login flow works correctly
- [ ] Auth state persists across page refreshes
- [ ] Logout functionality works
- [ ] Protected routes redirect to login when not authenticated
- [ ] Auth context provides user data correctly

## ✅ API Integration

- [ ] API calls to backend work
- [ ] API proxy configuration works (`/api/*` → `http://localhost:8000/api/*`)
- [ ] Error handling works for failed API calls
- [ ] Loading states display correctly

## ✅ UI & Styling

- [ ] Tailwind CSS styles are applied
- [ ] Components render correctly
- [ ] Responsive design works on mobile
- [ ] No layout shift or hydration errors
- [ ] Dark mode (if applicable) works

## ✅ State Management

- [ ] Zustand stores work correctly
- [ ] React Query caching works
- [ ] State persists where expected
- [ ] Global state updates correctly

## ✅ Forms & Validation

- [ ] Forms submit correctly
- [ ] Validation works (React Hook Form + Zod)
- [ ] Error messages display
- [ ] Form state resets after submission

## ✅ Data Fetching

- [ ] React Query hooks work
- [ ] Loading states display
- [ ] Error states display
- [ ] Data refetching works

## ✅ Performance

- [ ] Initial page load is fast
- [ ] Navigation between pages is smooth
- [ ] No unnecessary re-renders
- [ ] Bundle size is reasonable

## ✅ Browser Compatibility

- [ ] Works in Chrome
- [ ] Works in Firefox
- [ ] Works in Safari
- [ ] Works in Edge

## ✅ Development Experience

- [ ] Hot reload works
- [ ] TypeScript autocomplete works
- [ ] ESLint catches errors
- [ ] Console shows helpful errors

## Common Issues & Solutions

### Issue: "Module not found" errors
**Solution**: Check that imports use `@/*` alias correctly

### Issue: Hydration errors
**Solution**: Add `'use client'` directive to components using browser APIs

### Issue: Infinite redirects
**Solution**: Check auth logic in `src/app/(dashboard)/layout.tsx`

### Issue: Styles not loading
**Solution**: Ensure CSS is imported in `src/app/layout.tsx`

### Issue: API calls failing
**Solution**: Check Next.js proxy configuration in `next.config.js`

### Issue: React Query not working
**Solution**: Verify QueryProvider is in the layout

## Testing Commands

```bash
# Start dev server
npm run dev

# Type check
npm run type-check

# Lint
npm run lint

# Run tests
npm test

# E2E tests
npm run test:e2e
```

## Next Steps After Testing

1. [ ] All tests passing
2. [ ] Remove old files: `rm -rf src/pages src/router src/App.tsx src/index.tsx`
3. [ ] Update navigation from React Router to Next.js
4. [ ] Optimize components for Next.js
5. [ ] Deploy to production

---

**Test thoroughly before removing old files!**
