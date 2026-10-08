"""
Authentication service for user management and security.

Supabase handles registration, login, token refresh, and password change /
reset. This service provides profile management, admin operations, and user
statistics.
"""

import asyncio
import hashlib
import hmac
import inspect
import secrets
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import Depends
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.database import get_db
from src.models.user import User, UserRole

logger = __import__("logging").getLogger(__name__)

_STALE_EMAIL_CLAIM_TTL_SECONDS = 60
_PROVIDER_EMAIL_LOOKUP_FAILURE_TTL_SECONDS = 5
_STALE_EMAIL_CLAIM_CACHE_LIMIT = 2048
_STALE_EMAIL_CLAIM_HASH_KEY = secrets.token_bytes(32)
_EMAIL_LOCK_TTL_SECONDS = 120
_EMAIL_LOCK_WAIT_SECONDS = 15


class _EmailReconciliationBackoff:
    """Bounded process-local backoff for disproved claims and local conflicts."""

    def __init__(self) -> None:
        self._entries: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _key(user_id: str, email: str) -> tuple[str, str]:
        digest = hmac.new(
            _STALE_EMAIL_CLAIM_HASH_KEY,
            email.strip().lower().encode(),
            hashlib.sha256,
        ).hexdigest()
        return user_id, digest

    def contains(self, user_id: str, email: str) -> bool:
        key = self._key(user_id, email)
        now = time.monotonic()
        with self._lock:
            for expired_key, expires_at in list(self._entries.items()):
                if expires_at <= now:
                    del self._entries[expired_key]
            expires_at = self._entries.get(key)
            if expires_at is None:
                return False
            self._entries.move_to_end(key)
            return True

    def remember(
        self,
        user_id: str,
        email: str,
        ttl_seconds: float = _STALE_EMAIL_CLAIM_TTL_SECONDS,
    ) -> None:
        key = self._key(user_id, email)
        now = time.monotonic()
        with self._lock:
            for expired_key, expires_at in list(self._entries.items()):
                if expires_at <= now:
                    del self._entries[expired_key]
            self._entries[key] = now + ttl_seconds
            self._entries.move_to_end(key)
            while len(self._entries) > _STALE_EMAIL_CLAIM_CACHE_LIMIT:
                self._entries.popitem(last=False)


_email_reconciliation_backoff = _EmailReconciliationBackoff()


@asynccontextmanager
async def _hold_shared_email_lock(user_id: str) -> AsyncIterator[bool]:
    """Take a Redis lease so different workers serialize the same subject."""
    from src.core.cli_token_revocation import get_redis_client, reset_redis_client

    client = await get_redis_client()
    if client is None:
        yield False
        return

    key = f"auth:email-reconciliation:{hashlib.sha256(user_id.encode()).hexdigest()}"
    token = secrets.token_urlsafe(24)
    deadline = time.monotonic() + _EMAIL_LOCK_WAIT_SECONDS
    acquired = False
    try:
        while time.monotonic() < deadline:
            acquired = bool(
                await client.set(key, token, nx=True, ex=_EMAIL_LOCK_TTL_SECONDS)
            )
            if acquired:
                break
            await asyncio.sleep(0.05)
    except Exception as exc:  # noqa: BLE001 - fail closed on lock-store errors
        reset_redis_client()
        logger.warning(
            "Email reconciliation lock unavailable for user %s (%s)",
            user_id,
            type(exc).__name__,
        )
        yield False
        return

    if not acquired:
        logger.warning("Email reconciliation lock timed out for user %s", user_id)
        yield False
        return

    try:
        yield True
    finally:
        try:
            await client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                key,
                token,
            )
        except Exception as exc:  # noqa: BLE001 - the lease expires if release fails
            reset_redis_client()
            logger.warning(
                "Email reconciliation lock release failed for user %s (%s)",
                user_id,
                type(exc).__name__,
            )


class _KeyedEmailReconciliationLocks:
    """Coalesce same-process work and serialize each subject across workers."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[asyncio.Lock, int]] = {}
        self._guard = threading.Lock()

    @asynccontextmanager
    async def hold(self, user_id: str) -> AsyncIterator[None]:
        key = user_id
        with self._guard:
            entry = self._entries.get(key)
            lock, users = entry if entry is not None else (asyncio.Lock(), 0)
            self._entries[key] = (lock, users + 1)
        try:
            async with lock:
                async with _hold_shared_email_lock(key) as acquired:
                    yield acquired
        finally:
            with self._guard:
                lock, users = self._entries[key]
                if users == 1:
                    del self._entries[key]
                else:
                    self._entries[key] = (lock, users - 1)


_email_reconciliation_locks = _KeyedEmailReconciliationLocks()


class AuthenticationError(Exception):
    """Authentication related errors"""

    pass


class AuthorizationError(Exception):
    """Authorization related errors"""

    pass


class RegistrationError(Exception):
    """Registration related errors (e.g. profile email already in use)"""

    pass


class AuthService:
    """Authentication service for user management.

    Supabase handles registration, login, token lifecycle, and password
    changes. This service provides profile updates and admin operations.
    """

    def __init__(self, db: AsyncSession = None):
        self.db = db

    @staticmethod
    async def _maybe_await(value):
        """Support older tests that patch async helpers with plain Mocks."""
        if inspect.isawaitable(value):
            return await value
        return value

    async def authenticate_user(self, email: str, password: str) -> User:
        """Legacy authentication flow retained for older unit tests."""
        allowed = await self._maybe_await(self._check_rate_limit(email))
        if allowed is False:
            raise AuthenticationError(
                "Too many login attempts. Please try again later."
            )

        user = await self._maybe_await(self._get_user_by_email(email))
        if user is None:
            raise AuthenticationError("Invalid credentials")
        if not getattr(user, "is_active", False):
            raise AuthenticationError("Account is disabled")

        from src.core.security import verify_password as _verify_password

        if not _verify_password(password, user.password_hash):
            raise AuthenticationError("Invalid credentials")

        return user

    async def create_user(
        self,
        email: str,
        password: str,
        first_name: str,
        last_name: str,
        organization_id: str,
    ) -> User:
        """Legacy user-creation flow retained for older unit tests."""
        is_valid = await self._maybe_await(
            self._validate_user_data(
                email=email,
                password=password,
                first_name=first_name,
                last_name=last_name,
                organization_id=organization_id,
            )
        )
        if is_valid is False:
            raise AuthenticationError("Invalid user data")

        is_available = await self._maybe_await(self._check_email_availability(email))
        if is_available is False:
            raise AuthenticationError("Email already exists")

        from src.core.security import get_password_hash as _get_password_hash

        password_hash = _get_password_hash(password)
        return await self._maybe_await(
            self._save_user(
                email=email,
                password_hash=password_hash,
                first_name=first_name,
                last_name=last_name,
                organization_id=organization_id,
            )
        )

    async def _get_user_by_email(self, email: str) -> Optional[User]:
        """Compatibility helper for legacy unit tests."""
        if self.db is None:
            return None

        if hasattr(self.db, "execute"):
            stmt = select(User).where(
                and_(User.email == email.lower(), User.is_deleted == False)
            )
            result = await self.db.execute(stmt)
            return result.scalars().first()

        query = self.db.query(User).filter(User.email == email.lower())
        return query.first()

    async def _check_rate_limit(self, _identifier: str) -> bool:
        """Compatibility helper for legacy unit tests."""
        return True

    async def _validate_user_data(self, **_kwargs) -> bool:
        """Compatibility helper for legacy unit tests."""
        return True

    async def _check_email_availability(self, email: str) -> bool:
        """Compatibility helper for legacy unit tests."""
        existing = await self._get_user_by_email(email)
        return existing is None

    async def _save_user(
        self,
        email: str,
        password_hash: str,
        first_name: str,
        last_name: str,
        organization_id: str,
    ) -> User:
        """Compatibility helper for legacy unit tests."""
        user = User(
            email=email.lower(),
            password_hash=password_hash,
            first_name=first_name,
            last_name=last_name,
            organization_id=organization_id,
            role=UserRole.USER,
        )

        if self.db is not None:
            self.db.add(user)
            if hasattr(self.db, "commit"):
                commit_result = self.db.commit()
                if inspect.isawaitable(commit_result):
                    await commit_result
            if hasattr(self.db, "refresh"):
                refresh_result = self.db.refresh(user)
                if inspect.isawaitable(refresh_result):
                    await refresh_result

        return user

    # NOTE: change_password() was retired along with POST /auth/change-password.
    # It gated on verify_password(current_password, user.password_hash), and under
    # hosted GoTrue that hash is only ever the random secret written by JIT
    # provisioning, so the gate could never pass. Supabase's reset-password email
    # is the supported flow.

    async def update_user_profile(
        self,
        user: User,
        first_name: str = None,
        last_name: str = None,
    ) -> User:
        """Update the caller-editable profile fields (display names only).

        ``users.email`` is deliberately NOT writable here (GOO-405). The
        identity provider owns it, and JIT provisioning writes it from the
        verified token. A client-supplied address allowed pre-registration
        squatting of another person's email.
        """
        if first_name:
            user.first_name = first_name
        if last_name:
            user.last_name = last_name

        await self.db.commit()
        await self.db.refresh(user)

        return user

    async def sync_user_email_from_provider(
        self, user: User, token_email: str
    ) -> Optional[User]:
        """Persist a changed email only after confirming GoTrue's current value.

        Access-token claims may be stale, so they are only a signal to perform
        the provider lookup. This service owns commit/rollback and never adopts
        another row by email if the unique constraint detects a collision.
        """
        claimed_email = token_email.strip().lower()
        if not claimed_email or user.email.lower() == claimed_email:
            return user

        subject_id = str(user.id)
        # get_current_user has only read the row at this point. End that read
        # transaction before waiting for a same-subject request or calling
        # Supabase, so neither wait holds a scarce pool connection.
        await self.db.rollback()
        async with _email_reconciliation_locks.hold(subject_id) as lock_acquired:
            if not lock_acquired:
                return await self._get_active_user(subject_id)
            current_user = await self._get_active_user(subject_id)
            if current_user is None:
                return None
            if current_user.email.lower() == claimed_email:
                return current_user
            if _email_reconciliation_backoff.contains(subject_id, claimed_email):
                return current_user

            # The row read above opened another transaction. Release it before
            # the provider round trip, then load the subject again afterward.
            await self.db.rollback()

            from src.core.user_provisioning import get_verified_supabase_email

            provider_email = await asyncio.to_thread(
                get_verified_supabase_email, subject_id
            )
            if not provider_email:
                _email_reconciliation_backoff.remember(
                    subject_id,
                    claimed_email,
                    ttl_seconds=_PROVIDER_EMAIL_LOOKUP_FAILURE_TTL_SECONDS,
                )
                return await self._get_active_user(subject_id)
            if provider_email != claimed_email:
                _email_reconciliation_backoff.remember(subject_id, claimed_email)

            current_user = await self._get_active_user(subject_id)
            if current_user is None or current_user.email.lower() == provider_email:
                return current_user

            current_user.email = provider_email
            try:
                await self.db.commit()
            except IntegrityError:
                _email_reconciliation_backoff.remember(subject_id, provider_email)
                await self.db.rollback()
                current_user = await self._get_active_user(subject_id)
                logger.warning(
                    "Current provider email sync for user %s conflicts with another "
                    "account; operator reconciliation required",
                    subject_id,
                )
            return current_user

    async def _get_active_user(self, subject_id: str) -> Optional[User]:
        result = await self.db.execute(
            select(User)
            .options(selectinload(User.organization))
            .where(
                User.id == subject_id,
                User.is_active == True,
                User.is_deleted == False,
            )
        )
        return result.scalars().first()

    async def update_user_role(
        self, admin_user: User, target_user: User, new_role: UserRole
    ) -> User:
        """Update user role (admin only)"""
        if not admin_user.has_permission(UserRole.ADMIN):
            raise AuthorizationError("Only admins can update user roles")

        # Prevent admins from demoting themselves unless they're the last admin
        if target_user.id == admin_user.id and new_role != UserRole.ADMIN:
            # Check if there are other admins in the organization
            stmt = select(func.count(User.id)).where(
                and_(
                    User.organization_id == admin_user.organization_id,
                    User.role == UserRole.ADMIN,
                    User.is_active == True,
                    User.is_deleted == False,
                )
            )
            result = await self.db.execute(stmt)
            admin_count = result.scalar()

            if admin_count <= 1:
                raise AuthorizationError("Cannot remove admin role from last admin")

        target_user.role = new_role
        await self.db.commit()
        await self.db.refresh(target_user)

        return target_user

    async def deactivate_user(self, admin_user: User, target_user: User) -> bool:
        """Deactivate a user (admin only)"""
        if not admin_user.has_permission(UserRole.ADMIN):
            raise AuthorizationError("Only admins can deactivate users")

        if target_user.id == admin_user.id:
            raise AuthorizationError("Cannot deactivate your own account")

        target_user.is_active = False
        await self.db.commit()

        # Also revoke any outstanding long-lived CLI tokens so a deactivated
        # user's token can't keep working until a chokepoint DB-rechecks
        # is_active. Best-effort (never blocks deactivation).
        try:
            from src.core.cli_token_revocation import revoke_user_cli_tokens

            await revoke_user_cli_tokens(str(target_user.id))
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "CLI token revocation on deactivate failed for %s: %s",
                target_user.id,
                e,
            )

        return True

    async def get_organization_users(
        self,
        organization_id: str,
        skip: int = 0,
        limit: int = 100,
        role: UserRole = None,
        is_active: bool = None,
    ) -> List[User]:
        """Get users in an organization"""
        stmt = select(User).where(
            and_(User.organization_id == organization_id, User.is_deleted == False)
        )

        if role:
            stmt = stmt.where(User.role == role)

        if is_active is not None:
            stmt = stmt.where(User.is_active == is_active)

        stmt = stmt.offset(skip).limit(limit)
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def get_user_statistics(self, organization_id: str) -> Dict[str, Any]:
        """Get user statistics for an organization"""
        stmt = select(func.count(User.id)).where(
            and_(User.organization_id == organization_id, User.is_deleted == False)
        )
        result = await self.db.execute(stmt)
        total_users = result.scalar()

        stmt = select(func.count(User.id)).where(
            and_(
                User.organization_id == organization_id,
                User.is_active == True,
                User.is_deleted == False,
            )
        )
        result = await self.db.execute(stmt)
        active_users = result.scalar()

        # Users by role
        role_stats = {}
        for role in UserRole:
            stmt = select(func.count(User.id)).where(
                and_(
                    User.organization_id == organization_id,
                    User.role == role,
                    User.is_deleted == False,
                )
            )
            result = await self.db.execute(stmt)
            count = result.scalar()
            role_stats[role.value] = count

        # Recent activity (users who logged in within last 30 days)
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
        stmt = select(func.count(User.id)).where(
            and_(
                User.organization_id == organization_id,
                User.last_login >= thirty_days_ago,
                User.is_deleted == False,
            )
        )
        result = await self.db.execute(stmt)
        recent_active = result.scalar()

        return {
            "total_users": total_users,
            "active_users": active_users,
            "inactive_users": total_users - active_users,
            "recent_active_users": recent_active,
            "users_by_role": role_stats,
        }

    async def cleanup_inactive_users(
        self, organization_id: str, days_inactive: int = 90
    ) -> int:
        """Soft delete users inactive for specified days"""
        cutoff_date = datetime.utcnow() - timedelta(days=days_inactive)

        stmt = select(User).where(
            and_(
                User.organization_id == organization_id,
                User.last_login < cutoff_date,
                User.is_deleted == False,
            )
        )
        result = await self.db.execute(stmt)
        inactive_users = result.scalars().all()

        for user in inactive_users:
            user.soft_delete()

        await self.db.commit()
        return len(inactive_users)


def get_auth_service(db: AsyncSession = Depends(get_db)) -> AuthService:
    """Get authentication service instance"""
    return AuthService(db)
