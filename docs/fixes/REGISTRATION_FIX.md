# Registration Fix - Duplicate Organization Names

## Problem

The registration endpoint was failing when multiple users tried to register with the same organization name (e.g., "Demo Organization"). This caused a PostgreSQL unique constraint violation:

```
psycopg2.errors.UniqueViolation: duplicate key value violates unique constraint "ix_organizations_name"
DETAIL: Key (name)=(Demo Organization) already exists.
```

### Root Cause

In `auth_service.py:175-188` (written against `rag/backend/src/services/auth_service.py`, which is no longer in the tree), when a user registered with an `organization_name`, the code would always attempt to create a new organization without checking if one with that name already existed.

```python
# OLD CODE (BROKEN)
elif organization_name:
    # Create new organization
    organization = Organization(
        name=organization_name,
        storage_tier=StorageTier.FREE,
        storage_limit_bytes=Organization.get_default_storage_limit(StorageTier.FREE),
        is_active=True
    )
    self.db.add(organization)
    self.db.flush()  # Get the organization ID

    # First user in organization becomes admin
    role = UserRole.ADMIN
```

## Solution

Modified the `register_user` method in `AuthService` to check if an organization with the given name already exists before attempting to create a new one:

```python
# NEW CODE (FIXED)
elif organization_name:
    # Check if organization already exists
    organization = self.db.query(Organization).filter(
        and_(
            Organization.name == organization_name,
            Organization.is_active == True,
            Organization.is_deleted == False
        )
    ).first()

    if not organization:
        # Create new organization
        organization = Organization(
            name=organization_name,
            storage_tier=StorageTier.FREE,
            storage_limit_bytes=Organization.get_default_storage_limit(StorageTier.FREE),
            is_active=True
        )
        self.db.add(organization)
        self.db.flush()  # Get the organization ID

        # First user in organization becomes admin
        role = UserRole.ADMIN
    # If organization exists, use default USER role (don't make them admin)
```

## Behavior

### Before Fix
- ❌ First user registers with "Demo Organization" → Organization created, user becomes ADMIN ✅
- ❌ Second user registers with "Demo Organization" → ERROR: Unique constraint violation ❌

### After Fix
- ✅ First user registers with "Demo Organization" → Organization created, user becomes ADMIN ✅
- ✅ Second user registers with "Demo Organization" → Joins existing organization as USER ✅
- ✅ Third user registers with "Demo Organization" → Joins existing organization as USER ✅

## Testing

Run the test script to verify the fix:

```bash
./test_registration_fix.sh
```

Expected output:
```
✅ User 1 registered successfully
✅ User 2 registered successfully (using existing org)
✅ User 3 registered successfully (using existing org)
🎉 All tests passed! The duplicate organization fix works!
```

## Impact

- **User Experience**: Users can now register with common organization names without errors
- **Multi-tenancy**: Multiple users can join the same organization by providing the same organization name
- **Role Assignment**: First user in a new organization becomes ADMIN, subsequent users become regular USER
- **Database Integrity**: No more unique constraint violations on organization names

## Files Changed

1. `backend/src/services/auth_service.py` (path from the original session; no longer in the tree) - Updated `register_user` method

## Test Files Created

1. `test_registration_fix.sh` - Bash script to test the fix
2. `test_registration_fix.py` - Python script to test the fix (requires requests library)

## Notes

- The fix maintains backward compatibility
- No database migrations required
- Existing organizations are unaffected
- The first user in a new organization still becomes ADMIN
- Users joining an existing organization become regular USER (not ADMIN)
