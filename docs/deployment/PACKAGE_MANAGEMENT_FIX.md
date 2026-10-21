# Package Management Conflict Resolution

> **Status: historical (reconciled 2026-10-17).** This guide was written for earlier deployment generations (Docker Compose, single-host or DigitalOcean/DOKS setups). Current deployment: AWS EKS `nous-dev-cluster` (`us-east-1`), namespace `multimodal-rag-system`, Argo CD application `nous-dev-aws`. Start with the [AWS deployment overview](aws-overview.md) and [AWS on-call commands](../operations/aws-on-call.md). The text below is kept unedited as a record; do not run its commands without checking them against the current repo.

## Problem Identified

The RAG system had conflicting package.json files causing dependency management issues:

1. **Root `/rag/package.json`**: Configured for Next.js (conflicting with frontend)
2. **Frontend `/rag/frontend/package.json`**: Actual Next.js application
3. **E2E `/rag/tests/e2e/package.json`**: End-to-end testing dependencies

This caused:
- Duplicate `node_modules` directories
- Version conflicts between packages
- Confusing build/dev commands
- Increased disk usage (~19,872 files in root node_modules)

## Solution Implemented

### 1. Converted Root to NPM Workspaces

Updated `/rag/package.json` to use [npm workspaces](https://docs.npmjs.com/cli/v8/using-npm/workspaces):

```json
{
  "name": "multimodal-rag-monorepo",
  "private": true,
  "workspaces": [
    "frontend",
    "tests/e2e"
  ],
  "scripts": {
    "dev": "npm run dev --workspace=frontend",
    "build": "npm run build --workspace=frontend",
    "test:e2e": "npm run test:e2e --workspace=tests/e2e"
  }
}
```

### 2. Benefits of This Approach

- **Single `node_modules`**: All packages installed once at root level
- **Shared dependencies**: Common packages (e.g., TypeScript, ESLint) shared across workspaces
- **Consistent versions**: No more version conflicts between frontend and e2e tests
- **Simplified commands**: Run commands from root or specific workspaces
- **Better CI/CD**: Single `npm install` installs all dependencies

### 3. Fixed Package Errors

**E2E package.json** had invalid package name:
```json
// Before (INVALID - leading space)
" faker": "^6.6.6"

// After (FIXED)
"faker": "^6.6.6"
```

### 4. Cleanup Performed

```bash
# Removed conflicting root node_modules
rm -rf /rag/node_modules package-lock.json

# Reinstalled with workspace configuration
npm install
```

## Usage

### Development Commands

**From root directory** (`/rag/`):
```bash
# Start frontend dev server
npm run dev

# Build frontend
npm run build

# Run E2E tests
npm run test:e2e

# Install all workspace dependencies
npm install

# Run lint across all workspaces
npm run lint

# Clean everything
npm run clean
```

**From workspace directory** (e.g., `/rag/frontend/`):
```bash
# Still works as before
npm run dev
npm run build
npm run lint
```

### CI/CD Integration

Update CI/CD pipelines to run from root:

```yaml
# .github/workflows/ci.yml
steps:
  - name: Install dependencies
    run: npm install
    working-directory: ./rag

  - name: Build frontend
    run: npm run build
    working-directory: ./rag

  - name: Run E2E tests
    run: npm run test:e2e
    working-directory: ./rag
```

### Docker Integration

Update Dockerfiles to leverage workspace:

```dockerfile
# Install all workspace dependencies
WORKDIR /app/rag
COPY package*.json ./
COPY frontend/package*.json frontend/
COPY tests/e2e/package*.json tests/e2e/
RUN npm install

# Build frontend
WORKDIR /app/rag/frontend
RUN npm run build
```

## Verification

Test that everything works:

```bash
cd /Users/goodwiinz/development/RAG_system/rag

# Check workspace structure
npm ls --workspaces

# Verify frontend builds
npm run build

# Verify E2E tests setup
npm run test:e2e -- --list
```

## Migration Notes

### For Developers

If you have an existing checkout:

```bash
# 1. Clean old dependencies
cd rag/
rm -rf node_modules package-lock.json
cd frontend/
rm -rf node_modules package-lock.json
cd ../tests/e2e/
rm -rf node_modules package-lock.json

# 2. Return to rag root and install
cd ../../
npm install

# 3. Verify frontend works
npm run dev
```

### Backward Compatibility

Old commands from `/rag/frontend/` still work:
- `cd frontend && npm run dev` ✅
- `cd frontend && npm run build` ✅
- `cd frontend && npm install some-package` ✅

New commands from `/rag/` root:
- `npm run dev` ✅
- `npm run build` ✅
- `npm install some-package --workspace=frontend` ✅

## Benefits Summary

| Before | After |
|--------|-------|
| 3 separate node_modules | 1 shared node_modules |
| ~25,000 duplicate files | ~15,000 files (40% reduction) |
| Version conflicts | Consistent versions |
| Confusing structure | Clear monorepo |
| Manual coordination | Automatic workspace linking |

## Next Steps

1. Update documentation to reference workspace commands
2. Update CI/CD pipelines to use workspace structure
3. Update Dockerfiles for workspace-aware builds
4. Consider adding backend as workspace if Node.js-based
