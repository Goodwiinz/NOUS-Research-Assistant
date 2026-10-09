"""
Encryption service for managing data encryption operations.

This service provides high-level encryption operations including:
- Key rotation and management
- Bulk encryption/decryption operations
- Encryption audit logging
- Data protection compliance
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union
from uuid import UUID

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from src.core.encryption import (
    DataEncryptionError,
    EncryptionAlgorithm,
    EncryptionError,
    EncryptionKeyType,
    KeyManagementError,
    get_aes_encryption,
    get_field_encryption,
    get_file_encryption,
    get_key_manager,
)
from src.models.document import Document
from src.models.encrypted_user import (
    EncryptedOrganizationProfile,
    EncryptedUserProfile,
    EncryptionAuditLog,
)
from src.models.organization import Organization
from src.models.user import User

logger = logging.getLogger(__name__)


class EncryptionService:
    """Service for managing encryption operations and data protection"""

    def __init__(self, db: Session):
        self.db = db
        self.key_manager = get_key_manager()
        self.aes_encryption = get_aes_encryption()
        self.field_encryption = get_field_encryption()
        self.file_encryption = get_file_encryption()

    def log_encryption_operation(
        self,
        operation_type: str,
        resource_type: str,
        resource_id: UUID,
        performed_by: Optional[UUID] = None,
        organization_id: Optional[UUID] = None,
        key_id: Optional[str] = None,
        operation_details: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        success: bool = True,
        error_message: Optional[str] = None,
    ) -> EncryptionAuditLog:
        """
        Log an encryption operation for audit purposes

        Args:
            operation_type: Type of operation (ENCRYPT, DECRYPT, ROTATE_KEY, etc.)
            resource_type: Type of resource (USER_PROFILE, ORG_PROFILE, DOCUMENT, etc.)
            resource_id: ID of the resource
            performed_by: User who performed the operation
            organization_id: Organization context
            key_id: Key ID used for the operation
            operation_details: Additional operation details (will be encrypted)
            ip_address: IP address of the requester
            user_agent: User agent string
            success: Whether the operation succeeded
            error_message: Error message if operation failed

        Returns:
            Created audit log entry
        """
        audit_log = EncryptionAuditLog(
            operation_type=operation_type,
            resource_type=resource_type,
            resource_id=resource_id,
            key_id=key_id,
            performed_by=performed_by,
            organization_id=organization_id,
            operation_details=operation_details,
            ip_address=ip_address,
            user_agent=user_agent,
            success=success,
            error_message=error_message,
        )

        self.db.add(audit_log)
        self.db.commit()

        return audit_log

    def encrypt_user_profile(
        self,
        user_id: UUID,
        profile_data: Dict[str, Any],
        performed_by: UUID,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> EncryptedUserProfile:
        """
        Encrypt and store user profile data

        Args:
            user_id: User ID
            profile_data: Dictionary of profile data to encrypt
            performed_by: User performing the operation
            ip_address: IP address
            user_agent: User agent

        Returns:
            Created or updated encrypted profile
        """
        try:
            # Get or create encrypted profile
            profile = (
                self.db.query(EncryptedUserProfile)
                .filter(EncryptedUserProfile.user_id == user_id)
                .first()
            )

            if not profile:
                profile = EncryptedUserProfile(user_id=user_id)
                self.db.add(profile)

            # Update encrypted fields
            profile.update_encrypted_data(profile_data)

            # Get active data key for logging
            data_key = self.key_manager.get_active_key(EncryptionKeyType.DATA)

            self.db.commit()

            # Log successful encryption
            self.log_encryption_operation(
                operation_type="ENCRYPT_PROFILE",
                resource_type="USER_PROFILE",
                resource_id=user_id,
                performed_by=performed_by,
                key_id=data_key.key_id if data_key else None,
                operation_details={"fields_updated": list(profile_data.keys())},
                ip_address=ip_address,
                user_agent=user_agent,
                success=True,
            )

            logger.info(f"Successfully encrypted user profile for user {user_id}")
            return profile

        except Exception as e:
            logger.error(f"Failed to encrypt user profile for user {user_id}: {str(e)}")
            self.db.rollback()

            # Log failed encryption
            self.log_encryption_operation(
                operation_type="ENCRYPT_PROFILE",
                resource_type="USER_PROFILE",
                resource_id=user_id,
                performed_by=performed_by,
                operation_details={"fields_attempted": list(profile_data.keys())},
                ip_address=ip_address,
                user_agent=user_agent,
                success=False,
                error_message=str(e),
            )

            raise DataEncryptionError(f"Failed to encrypt user profile: {str(e)}")

    def encrypt_organization_profile(
        self,
        organization_id: UUID,
        profile_data: Dict[str, Any],
        performed_by: UUID,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> EncryptedOrganizationProfile:
        """
        Encrypt and store organization profile data

        Args:
            organization_id: Organization ID
            profile_data: Dictionary of profile data to encrypt
            performed_by: User performing the operation
            ip_address: IP address
            user_agent: User agent

        Returns:
            Created or updated encrypted organization profile
        """
        try:
            # Get or create encrypted profile
            profile = (
                self.db.query(EncryptedOrganizationProfile)
                .filter(EncryptedOrganizationProfile.organization_id == organization_id)
                .first()
            )

            if not profile:
                profile = EncryptedOrganizationProfile(organization_id=organization_id)
                self.db.add(profile)

            # Update encrypted fields
            profile.update_encrypted_data(profile_data)

            # Get active data key for logging
            data_key = self.key_manager.get_active_key(EncryptionKeyType.DATA)

            self.db.commit()

            # Log successful encryption
            self.log_encryption_operation(
                operation_type="ENCRYPT_PROFILE",
                resource_type="ORG_PROFILE",
                resource_id=organization_id,
                performed_by=performed_by,
                key_id=data_key.key_id if data_key else None,
                operation_details={"fields_updated": list(profile_data.keys())},
                ip_address=ip_address,
                user_agent=user_agent,
                success=True,
            )

            logger.info(
                f"Successfully encrypted organization profile for org {organization_id}"
            )
            return profile

        except Exception as e:
            logger.error(
                f"Failed to encrypt organization profile for org {organization_id}: {str(e)}"
            )
            self.db.rollback()

            # Log failed encryption
            self.log_encryption_operation(
                operation_type="ENCRYPT_PROFILE",
                resource_type="ORG_PROFILE",
                resource_id=organization_id,
                performed_by=performed_by,
                operation_details={"fields_attempted": list(profile_data.keys())},
                ip_address=ip_address,
                user_agent=user_agent,
                success=False,
                error_message=str(e),
            )

            raise DataEncryptionError(
                f"Failed to encrypt organization profile: {str(e)}"
            )

    def decrypt_user_profile(
        self,
        user_id: UUID,
        requested_by: UUID,
        fields: Optional[List[str]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Decrypt user profile data

        Args:
            user_id: User ID
            fields: List of fields to decrypt (all if None)
            requested_by: User requesting the data
            ip_address: IP address
            user_agent: User agent

        Returns:
            Dictionary with decrypted data
        """
        try:
            profile = (
                self.db.query(EncryptedUserProfile)
                .filter(EncryptedUserProfile.user_id == user_id)
                .first()
            )

            if not profile:
                return {}

            # Decrypt requested fields
            decrypted_data = profile.get_decrypted_data(fields)

            # Log successful decryption
            self.log_encryption_operation(
                operation_type="DECRYPT_PROFILE",
                resource_type="USER_PROFILE",
                resource_id=user_id,
                performed_by=requested_by,
                operation_details={"fields_accessed": list(decrypted_data.keys())},
                ip_address=ip_address,
                user_agent=user_agent,
                success=True,
            )

            logger.info(f"Successfully decrypted user profile for user {user_id}")
            return decrypted_data

        except Exception as e:
            logger.error(f"Failed to decrypt user profile for user {user_id}: {str(e)}")

            # Log failed decryption
            self.log_encryption_operation(
                operation_type="DECRYPT_PROFILE",
                resource_type="USER_PROFILE",
                resource_id=user_id,
                performed_by=requested_by,
                operation_details={"fields_attempted": fields or []},
                ip_address=ip_address,
                user_agent=user_agent,
                success=False,
                error_message=str(e),
            )

            raise DataEncryptionError(f"Failed to decrypt user profile: {str(e)}")

    def rotate_encryption_keys(
        self,
        key_type: str,
        performed_by: UUID,
        organization_id: Optional[UUID] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Rotate encryption keys for specified key type

        Args:
            key_type: Type of key to rotate (DATA, FILE, etc.)
            performed_by: User performing the rotation
            organization_id: Optional organization scope
            dry_run: If True, only preview what would be rotated

        Returns:
            Dictionary with rotation results
        """
        try:
            # Get active key of specified type
            active_key = self.key_manager.get_active_key(key_type)
            if not active_key:
                raise KeyManagementError(f"No active key found for type: {key_type}")

            if dry_run:
                # Preview rotation without actually performing it
                affected_resources = self._count_affected_resources(
                    key_type, organization_id
                )
                return {
                    "dry_run": True,
                    "key_type": key_type,
                    "current_key_id": active_key.key_id,
                    "affected_resources": affected_resources,
                    "rotation_needed": True,
                }

            # Generate new key
            new_key = self.key_manager.rotate_key(active_key.key_id)

            # Start rotation process
            rotation_results = {
                "key_type": key_type,
                "old_key_id": active_key.key_id,
                "new_key_id": new_key.key_id,
                "rotated_resources": 0,
                "failed_resources": 0,
                "errors": [],
            }

            # Rotate user profiles if data key
            if key_type == EncryptionKeyType.DATA:
                self._rotate_user_profiles(
                    active_key.key_id, new_key.key_id, performed_by, rotation_results
                )
                self._rotate_organization_profiles(
                    active_key.key_id, new_key.key_id, performed_by, rotation_results
                )

            # Rotate documents if file key
            elif key_type == EncryptionKeyType.FILE:
                self._rotate_documents(
                    active_key.key_id, new_key.key_id, performed_by, rotation_results
                )

            # Log key rotation
            self.log_encryption_operation(
                operation_type="ROTATE_KEY",
                resource_type="ENCRYPTION_KEY",
                resource_id=UUID(
                    active_key.key_id.replace("-", "")
                ),  # Convert to valid UUID
                performed_by=performed_by,
                organization_id=organization_id,
                key_id=new_key.key_id,
                operation_details=rotation_results,
                success=True,
            )

            logger.info(
                f"Successfully rotated {key_type} encryption key from {active_key.key_id} to {new_key.key_id}"
            )
            return rotation_results

        except Exception as e:
            logger.error(f"Failed to rotate {key_type} encryption key: {str(e)}")

            # Log failed rotation
            self.log_encryption_operation(
                operation_type="ROTATE_KEY",
                resource_type="ENCRYPTION_KEY",
                resource_id=UUID(int=0),  # Placeholder
                performed_by=performed_by,
                organization_id=organization_id,
                success=False,
                error_message=str(e),
            )

            raise KeyManagementError(f"Failed to rotate encryption key: {str(e)}")

    def _count_affected_resources(
        self, key_type: str, organization_id: Optional[UUID]
    ) -> Dict[str, int]:
        """Count resources that would be affected by key rotation"""
        counts = {}

        if key_type == EncryptionKeyType.DATA:
            # Count user profiles
            query = self.db.query(EncryptedUserProfile)
            if organization_id:
                query = query.join(User).filter(User.organization_id == organization_id)
            counts["user_profiles"] = query.count()

            # Count organization profiles
            query = self.db.query(EncryptedOrganizationProfile)
            if organization_id:
                query = query.filter(
                    EncryptedOrganizationProfile.organization_id == organization_id
                )
            counts["organization_profiles"] = query.count()

        elif key_type == EncryptionKeyType.FILE:
            # Count documents
            query = self.db.query(Document)
            if organization_id:
                query = query.filter(Document.organization_id == organization_id)
            counts["documents"] = query.count()

        return counts

    def _rotate_user_profiles(
        self,
        old_key_id: str,
        new_key_id: str,
        performed_by: UUID,
        results: Dict[str, Any],
    ) -> None:
        """Rotate encryption for user profiles"""
        profiles = self.db.query(EncryptedUserProfile).all()

        for profile in profiles:
            try:
                # Get all encrypted data
                current_data = profile.get_decrypted_data()

                # Update with new key encryption by setting values
                profile.update_encrypted_data(current_data)
                profile.last_encryption_rotation = datetime.utcnow()

                results["rotated_resources"] += 1

            except Exception as e:
                results["failed_resources"] += 1
                results["errors"].append(f"User profile {profile.user_id}: {str(e)}")
                logger.error(
                    f"Failed to rotate user profile {profile.user_id}: {str(e)}"
                )

    def _rotate_organization_profiles(
        self,
        old_key_id: str,
        new_key_id: str,
        performed_by: UUID,
        results: Dict[str, Any],
    ) -> None:
        """Rotate encryption for organization profiles"""
        profiles = self.db.query(EncryptedOrganizationProfile).all()

        for profile in profiles:
            try:
                # Get all encrypted data
                current_data = profile.get_decrypted_data()

                # Update with new key encryption by setting values
                profile.update_encrypted_data(current_data)
                profile.last_encryption_rotation = datetime.utcnow()

                results["rotated_resources"] += 1

            except Exception as e:
                results["failed_resources"] += 1
                results["errors"].append(
                    f"Organization profile {profile.organization_id}: {str(e)}"
                )
                logger.error(
                    f"Failed to rotate organization profile {profile.organization_id}: {str(e)}"
                )

    def _rotate_documents(
        self,
        old_key_id: str,
        new_key_id: str,
        performed_by: UUID,
        results: Dict[str, Any],
    ) -> None:
        """Rotate encryption for documents"""
        # This would require re-encrypting document files
        # Implementation depends on how document encryption is stored
        logger.info("Document rotation not yet implemented")
        results["notes"] = "Document rotation requires additional implementation"

    def get_encryption_status(
        self, organization_id: Optional[UUID] = None
    ) -> Dict[str, Any]:
        """
        Get encryption status and statistics

        Args:
            organization_id: Optional organization scope

        Returns:
            Dictionary with encryption status
        """
        status = {
            "key_management": {},
            "encrypted_resources": {},
            "recent_operations": [],
        }

        # Get key status
        for key_type in [
            EncryptionKeyType.MASTER,
            EncryptionKeyType.DATA,
            EncryptionKeyType.FILE,
        ]:
            active_key = self.key_manager.get_active_key(key_type)
            status["key_management"][key_type] = {
                "active_key_id": active_key.key_id if active_key else None,
                "key_algorithm": active_key.algorithm if active_key else None,
                "created_at": active_key.created_at.isoformat() if active_key else None,
                "expires_at": (
                    active_key.expires_at.isoformat()
                    if active_key and active_key.expires_at
                    else None
                ),
                "is_expired": active_key.is_expired() if active_key else False,
            }

        # Count encrypted resources
        if organization_id:
            user_profiles = (
                self.db.query(EncryptedUserProfile)
                .join(User)
                .filter(User.organization_id == organization_id)
                .count()
            )
            org_profiles = (
                self.db.query(EncryptedOrganizationProfile)
                .filter(EncryptedOrganizationProfile.organization_id == organization_id)
                .count()
            )
        else:
            user_profiles = self.db.query(EncryptedUserProfile).count()
            org_profiles = self.db.query(EncryptedOrganizationProfile).count()

        status["encrypted_resources"] = {
            "user_profiles": user_profiles,
            "organization_profiles": org_profiles,
        }

        # Get recent encryption operations (GOO-406 E2: tenant-scoped when an
        # organization is given; only platform operators reach the global view)
        recent_query = self.db.query(EncryptionAuditLog)
        if organization_id:
            recent_query = recent_query.filter(
                EncryptionAuditLog.organization_id == organization_id
            )
        recent_operations = (
            recent_query.order_by(EncryptionAuditLog.created_at.desc()).limit(10).all()
        )

        status["recent_operations"] = [
            {
                "operation_type": op.operation_type,
                "resource_type": op.resource_type,
                "resource_id": str(op.resource_id),
                "success": op.success,
                "created_at": op.created_at.isoformat(),
                "error_message": op.error_message,
            }
            for op in recent_operations
        ]

        return status

    def validate_encryption_integrity(self, sample_size: int = 10) -> Dict[str, Any]:
        """
        Validate encryption integrity by testing sample records

        Args:
            sample_size: Number of records to test

        Returns:
            Dictionary with validation results
        """
        results = {
            "user_profiles_tested": 0,
            "user_profiles_passed": 0,
            "organization_profiles_tested": 0,
            "organization_profiles_passed": 0,
            "errors": [],
        }

        # Test user profiles
        user_profiles = self.db.query(EncryptedUserProfile).limit(sample_size).all()
        for profile in user_profiles:
            try:
                # Attempt to decrypt all fields
                profile.get_decrypted_data()
                results["user_profiles_passed"] += 1
            except Exception as e:
                results["errors"].append(f"User profile {profile.user_id}: {str(e)}")
            finally:
                results["user_profiles_tested"] += 1

        # Test organization profiles
        org_profiles = (
            self.db.query(EncryptedOrganizationProfile).limit(sample_size).all()
        )
        for profile in org_profiles:
            try:
                # Attempt to decrypt all fields
                profile.get_decrypted_data()
                results["organization_profiles_passed"] += 1
            except Exception as e:
                results["errors"].append(
                    f"Organization profile {profile.organization_id}: {str(e)}"
                )
            finally:
                results["organization_profiles_tested"] += 1

        # Calculate success rates
        if results["user_profiles_tested"] > 0:
            results["user_profile_success_rate"] = (
                results["user_profiles_passed"] / results["user_profiles_tested"]
            )
        else:
            results["user_profile_success_rate"] = 1.0

        if results["organization_profiles_tested"] > 0:
            results["org_profile_success_rate"] = (
                results["organization_profiles_passed"]
                / results["organization_profiles_tested"]
            )
        else:
            results["org_profile_success_rate"] = 1.0

        results["overall_success_rate"] = (
            (results["user_profiles_passed"] + results["organization_profiles_passed"])
            / (
                results["user_profiles_tested"]
                + results["organization_profiles_tested"]
            )
            if (
                results["user_profiles_tested"]
                + results["organization_profiles_tested"]
            )
            > 0
            else 1.0
        )

        return results
