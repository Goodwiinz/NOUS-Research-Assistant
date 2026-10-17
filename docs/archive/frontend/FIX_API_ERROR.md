> **Archived 2026-10-17: one-off troubleshooting note for an old API error; paths and ports are outdated.**
> Historical record, not a live contract. Originally at `frontend/FIX_API_ERROR.md`; relative links and paths below were not updated.

# Fixing API Error in Frontend

## Problem Identified

The frontend is getting an `APIError` when trying to fetch documents because:

1. **Environment Variable Mismatch**: Frontend was using Create React App conventions (`REACT_APP_*`) instead of Next.js conventions (`NEXT_PUBLIC_*`)
2. **Authentication Required**: Backend API requires authentication but frontend might not be sending proper auth headers
3. **API Endpoint Response**: Backend returns `{"error":{"message":"Not authenticated","status_code":403,"type":"http_error"}}`

## ✅ Fixes Applied

### 1. Fixed Environment Variables

**Updated `frontend/.env.local`:**
```bash
# Changed from REACT_APP_* to NEXT_PUBLIC_*
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
NEXT_PUBLIC_WS_URL=ws://localhost:8000/ws
```

**Updated `frontend/src/types/api.ts`:**
```typescript
export const API_CONFIG = {
  BASE_URL: process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000',
  // ...
};
```

### 2. API Error Handling

The error occurs in `apiClient.ts:127` when trying to create an `APIErrorClass`. This suggests the backend is returning an error response that the frontend is trying to parse.

## 🚀 Quick Solutions

### Option 1: Restart Frontend with New Environment Variables

```bash
# Stop frontend (if running)
pkill -f "next dev"

# Restart frontend to pick up new environment variables
cd frontend
npm run dev
```

### Option 2: Check Authentication Status

```bash
# Check if you're logged in
# Open browser to http://localhost:3000
# Go to login page and authenticate
```

### Option 3: Test API Directly

```bash
# Test API without authentication (should show 403 error)
curl http://localhost:8000/api/v1/documents/

# Test API health endpoint
curl http://localhost:8000/health
```

## 🔍 Debugging Steps

### 1. Check Browser Console

Open browser dev tools and check:
- Network tab for failed requests
- Console tab for error messages
- Application tab for auth tokens

### 2. Verify Environment Variables

```bash
# In frontend directory, check Next.js environment
cd frontend
npm run dev

# In browser console, check:
console.log(process.env.NEXT_PUBLIC_API_BASE_URL);
```

### 3. Check Authentication Status

The API client should be automatically adding auth headers. Check if:

```javascript
// In browser console
import { useAuthStore } from '@/stores/authStore';
const authState = useAuthStore.getState();
console.log('Auth token:', authState.token);
console.log('Is authenticated:', authState.isAuthenticated);
```

## 🛠️ Manual Fixes

### If Environment Variables Aren't Loading

1. **Restart Next.js development server**
2. **Clear Next.js cache**:
   ```bash
   rm -rf .next
   npm run dev
   ```

### If Authentication Headers Are Missing

Check `apiClient.ts` is properly setting auth headers:

```typescript
// In src/services/apiClient.ts, line ~29
const authState = useAuthStore.getState();
const token = authState.token;

if (token) {
  config.headers.Authorization = `Bearer ${token}`;
}
```

### If APIErrorClass Constructor Fails

The `APIErrorClass` expects a specific error structure. Check if backend response matches:

```typescript
interface APIError {
  error: {
    message: string;
    status_code: number;
    type: string;
    // ...
  };
}
```

## 🎯 Next Steps

1. **Restart frontend** with new environment variables
2. **Clear browser cache** and reload the page
3. **Login to the application** if not already authenticated
4. **Test document upload** functionality
5. **Check monitoring** to see API calls succeeding

## 📊 Verification

After fixes applied, you should see:

- ✅ Environment variables loaded correctly
- ✅ Auth headers present in API requests
- ✅ Documents API calls returning data instead of errors
- ✅ Monitoring dashboards showing successful API calls

```bash
# Check frontend is loading correct API URL
curl http://localhost:3000/api/health

# Check API calls are working in browser network tab
# Should see successful calls to /api/v1/documents/
```

The API error should be resolved after applying these fixes! 🎉