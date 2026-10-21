> **Archived 2026-10-17: completed restructuring plan from the Next.js migration.**
> Historical record, not a live contract. Originally at `frontend/RESTRUCTURE_PLAN.md`; relative links and paths below were not updated.

# Next.js Full Structure Reorganization Plan

## New Structure (Option 2)

```
frontend/
├── src/
│   ├── app/                    # Next.js App Router (routes)
│   │   ├── layout.tsx         # Root layout
│   │   ├── page.tsx           # Home/redirect page
│   │   ├── (auth)/            # Auth route group
│   │   │   └── login/
│   │   │       └── page.tsx
│   │   └── (dashboard)/       # Protected routes group
│   │       ├── layout.tsx     # Dashboard layout
│   │       ├── dashboard/
│   │       ├── documents/
│   │       ├── search/
│   │       ├── graph/
│   │       ├── analytics/
│   │       ├── evaluation/
│   │       ├── ab-testing/
│   │       ├── monitoring/
│   │       └── settings/
│   ├── components/            # Shared UI components
│   │   ├── ui/               # Base UI components
│   │   ├── layout/           # Layout components
│   │   ├── documents/        # Feature-specific
│   │   ├── search/
│   │   ├── graph/
│   │   └── ...
│   ├── lib/                   # Utilities & helpers
│   │   ├── api.ts            # API client
│   │   ├── utils.ts          # Helper functions
│   │   └── constants.ts
│   ├── hooks/                 # Custom React hooks
│   ├── types/                 # TypeScript types
│   ├── stores/                # State management (Zustand)
│   └── styles/                # Global styles
├── public/                    # Static assets
├── tests/                     # Test files
└── ...config files
```

## Migration Steps

### Step 1: Reorganize app/ directory with route groups
- Create route groups: (auth) and (dashboard)
- Move pages to proper app router structure
- Add layouts for route groups

### Step 2: Keep components, hooks, types, lib, stores as-is
- These don't need major changes
- Just update imports if needed

### Step 3: Remove old structure
- Delete src/pages/ (after migration)
- Delete src/router/ (replaced by app router)
- Delete src/index.tsx (not needed)
- Delete src/App.tsx (replaced by layout)

### Step 4: Update imports
- Update all import paths to use new structure
- Ensure @/* aliases work correctly
