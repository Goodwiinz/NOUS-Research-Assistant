import asyncio
import logging
import secrets
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.security import TokenData, get_password_hash
from src.models.organization import Organization, StorageTier
from src.models.user import User, UserRole

logger = logging.getLogger(__name__)


class SupabaseEmailLookupError(Exception):
    """The provider could not be reached to confirm an account email."""


_FREE_STORAGE = Organization.get_default_storage_limit(StorageTier.FREE)

# Org names the provisioner mints itself and then RESOLVES BY NAME. A
# client-supplied sign-up name that squatted one of these could hand a later
# user another tenant's organization, so those prefixes are never usable as a
# sign-up org name.
_RESERVED_ORG_NAME_PREFIXES = ("user-", "org-")

# Second line of defense behind ``security._clean_metadata_string``: whatever
# reaches here still gets clamped to the column widths (users.first_name /
# last_name String(100), organizations.name String(255)).
_NAME_MAX_LENGTH = 100
_ORG_NAME_MAX_LENGTH = 255


async def ensure_user_and_org(
    db: AsyncSession,
    token_data: TokenData,
) -> Optional[User]:
    """JIT-provision User + Organization on first authenticated request.
    Returns existing or newly created User, or None on failure."""
    if not token_data.user_id:
        return None

    result = await db.execute(select(User).where(User.id == token_data.user_id))
    existing = result.scalars().first()
    if existing:
        return existing

    # A first-login row cannot safely take its email from a JWT snapshot: the
    # subject may present an unexpired token minted before a confirmed change.
    # End the empty-row read transaction before the provider round trip, then
    # create the row only from the provider's current confirmed address.
    await db.rollback()
    if getattr(token_data, "is_cli", False):
        return None
    verified_email = await asyncio.to_thread(
        get_verified_supabase_email, str(token_data.user_id)
    )
    if not verified_email:
        return None

    org = await _resolve_or_create_org(db, token_data)
    return await _create_user(db, token_data, org, verified_email=verified_email)


def get_verified_supabase_email(user_id: str) -> Optional[str]:
    """Return the provider's current confirmed email for a subject.

    JWT email claims are snapshots and can outlive an address change. This
    server-side admin lookup supplies the email for first-login provisioning
    and reconciles changed non-CLI token claims. Keep the network operation
    synchronous here so callers can move it to a worker thread without blocking
    the event loop.
    """
    try:
        from src.core.supabase_client import get_supabase_client

        client = get_supabase_client()
        if client is None:
            raise SupabaseEmailLookupError(
                "Supabase admin client is temporarily unavailable"
            )
        response = client.auth.admin.get_user_by_id(user_id)
        provider_user = response.user
        if provider_user is None or provider_user.email_confirmed_at is None:
            return None
        email = (provider_user.email or "").strip().lower()
        return email or None
    except SupabaseEmailLookupError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Current Supabase email lookup failed for user %s: %s",
            user_id,
            type(exc).__name__,
        )
        raise SupabaseEmailLookupError(
            "Current Supabase email could not be confirmed"
        ) from exc


async def _resolve_or_create_org(
    db: AsyncSession,
    token_data: TokenData,
) -> Optional[Organization]:
    org_id = token_data.organization_id

    if org_id:
        result = await db.execute(select(Organization).where(Organization.id == org_id))
        org = result.scalars().first()
        if org:
            return org
        org = Organization(
            id=org_id,
            name=f"org-{str(org_id)[:8]}",
            storage_tier=StorageTier.FREE,
            storage_used_bytes=0,
            storage_limit_bytes=_FREE_STORAGE,
            is_active=True,
            is_deleted=False,
        )
        db.add(org)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            result = await db.execute(
                select(Organization).where(Organization.id == org_id)
            )
            org = result.scalars().first()
        return org
    else:
        # Fail-closed: no org claim on the token. Give this org-less user
        # their OWN organization instead of funneling everyone into one
        # shared "Default Organization" — two org-less users must never
        # collapse into the same tenant scope (organization_id is what all
        # tenant filtering keys off). The FULL user_id is the org identity
        # here (the name is what we select on, unlike the org_id branch
        # where the PK is identity), so it must not be truncated — two ids
        # sharing a prefix would otherwise co-mingle. A full UUID name
        # (~54 chars) fits Organization.name (String(255), unique). Name is
        # deterministic on user_id so re-provisioning (or a concurrent
        # duplicate) resolves back to the same org.
        fallback_name = f"user-{token_data.user_id} Organization"
        result = await db.execute(
            select(Organization).where(Organization.name == fallback_name)
        )
        org = result.scalars().first()
        if org:
            return org

        # The org the user typed at sign-up is a LABEL for the org we are
        # about to create, never a lookup key: resolving an existing org by a
        # client-supplied name would let anyone join another tenant just by
        # typing that tenant's name. If the name is already taken (or is a
        # reserved one) we silently fall back to the deterministic name — a
        # cosmetic loss, versus a tenant breach.
        for candidate in _org_name_candidates(token_data, fallback_name):
            org = Organization(
                name=candidate,
                storage_tier=StorageTier.FREE,
                storage_used_bytes=0,
                storage_limit_bytes=_FREE_STORAGE,
                is_active=True,
                is_deleted=False,
            )
            db.add(org)
            try:
                await db.flush()
                return org
            except IntegrityError:
                # Either a concurrent request created THIS user's org, or the
                # sign-up name belongs to somebody else. Only the deterministic
                # name may be refetched.
                await db.rollback()
                result = await db.execute(
                    select(Organization).where(Organization.name == fallback_name)
                )
                existing = result.scalars().first()
                if existing:
                    return existing

        return None


def _org_name_candidates(token_data: TokenData, fallback_name: str) -> list[str]:
    """Org names to try, best first: the signed-up label, then the fallback."""
    desired = token_data.signup_organization_name
    if desired and not desired.lower().startswith(_RESERVED_ORG_NAME_PREFIXES):
        return [desired[:_ORG_NAME_MAX_LENGTH], fallback_name]
    return [fallback_name]


async def _create_user(
    db: AsyncSession,
    token_data: TokenData,
    org: Optional[Organization],
    *,
    verified_email: str,
) -> Optional[User]:
    email = verified_email.strip().lower()
    prefix = email.split("@")[0]
    parts = prefix.split(".")
    # Names the user typed at sign-up (Supabase user_metadata) win; guessing
    # from the email local-part stays the fallback when they're absent/blank.
    first_name = (token_data.signup_first_name or parts[0].capitalize())[
        :_NAME_MAX_LENGTH
    ]
    last_name = (
        token_data.signup_last_name
        or (parts[-1].capitalize() if len(parts) > 1 else "User")
    )[:_NAME_MAX_LENGTH]

    user = User(
        id=token_data.user_id,
        email=email,
        password_hash=get_password_hash(secrets.token_urlsafe(32)),
        first_name=first_name,
        last_name=last_name,
        role=UserRole.USER,
        is_active=True,
        is_deleted=False,
        organization_id=str(org.id) if org else None,
    )
    db.add(user)
    try:
        await db.flush()
        logger.info(
            f"JIT-provisioned user {token_data.user_id} "
            f"in org {org.id if org else 'none'}"
        )
    except IntegrityError:
        await db.rollback()
        result = await db.execute(select(User).where(User.id == token_data.user_id))
        user = result.scalars().first()
        if user is None:
            # The INSERT conflicted, but not with a row for this subject. In
            # practice another row already holds this verified address in
            # users.email (UNIQUE): a stale row, or a squat via the pre-GOO-405
            # PUT /auth/me. Do NOT add a fallback lookup by email that adopts
            # that row. Whoever wrote that email would then get this person's
            # identity, which turns squatting into account takeover. Fail
            # closed (the caller returns 401) and leave reconciliation to an
            # operator. Log the subject id, never the email (PII).
            logger.warning(
                "JIT provisioning for user %s failed: unique conflict on a row "
                "that is not this user's (likely users.email held by another "
                "account); operator reconciliation required",
                token_data.user_id,
            )

    return user
