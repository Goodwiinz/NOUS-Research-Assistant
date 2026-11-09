"""
Encrypted user model with field-level encryption for sensitive data.

This module extends the base user model with encryption capabilities
for PII (Personally Identifiable Information) and other sensitive data.
"""

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import relationship

from .base import Base
from .encrypted_fields import (
    EncryptedAddress,
    EncryptedEmail,
    EncryptedJSON,
    EncryptedPhone,
    EncryptedString,
    EncryptedText,
)


class EncryptedUserProfile(Base):
    """
    Encrypted user profile containing sensitive personal information.

    This table stores PII with field-level encryption while maintaining
    searchability through non-encrypted reference fields.
    """

    __tablename__ = "encrypted_user_profiles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    # Basic encrypted personal information
    first_name = Column(EncryptedString)
    last_name = Column(EncryptedString)
    middle_name = Column(EncryptedString)

    # Contact information (encrypted)
    email_personal = Column(EncryptedEmail)
    phone_mobile = Column(EncryptedPhone)
    phone_work = Column(EncryptedPhone)
    address_home = Column(EncryptedAddress)
    address_work = Column(EncryptedAddress)

    # Sensitive identifiers
    ssn = Column(EncryptedString)  # Social Security Number
    passport_number = Column(EncryptedString)
    driver_license = Column(EncryptedString)

    # Emergency contact (encrypted)
    emergency_contact_name = Column(EncryptedString)
    emergency_contact_phone = Column(EncryptedPhone)
    emergency_contact_relationship = Column(EncryptedString)

    # Additional encrypted personal data
    personal_notes = Column(EncryptedText)
    preferences = Column(EncryptedJSON)

    # Employment information (encrypted)
    job_title = Column(EncryptedString)
    department = Column(EncryptedString)
    employee_id = Column(EncryptedString)

    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate="now()")
    last_encryption_rotation = Column(DateTime(timezone=True))

    # Relationships
    user = relationship("User", back_populates="encrypted_profile")

    def __repr__(self):
        return f"<EncryptedUserProfile(user_id={self.user_id})>"

    @hybrid_property
    def full_name(self) -> Optional[str]:
        """Get full name (decrypted)"""
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.first_name or self.last_name

    def get_decrypted_data(self, fields: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Get decrypted user data for specified fields

        Args:
            fields: List of field names to decrypt (all if None)

        Returns:
            Dictionary with decrypted data
        """
        if fields is None:
            fields = [
                "first_name",
                "last_name",
                "middle_name",
                "email_personal",
                "phone_mobile",
                "phone_work",
                "address_home",
                "address_work",
                "ssn",
                "passport_number",
                "driver_license",
                "emergency_contact_name",
                "emergency_contact_phone",
                "emergency_contact_relationship",
                "personal_notes",
                "preferences",
                "job_title",
                "department",
                "employee_id",
            ]

        result = {}
        for field in fields:
            if hasattr(self, field):
                value = getattr(self, field)
                result[field] = value

        return result

    def update_encrypted_data(self, data: Dict[str, Any]) -> None:
        """
        Update encrypted fields with new data

        Args:
            data: Dictionary of field names and values to update
        """
        for field, value in data.items():
            if hasattr(self, field):
                setattr(self, field, value)

    def mask_sensitive_fields(self) -> Dict[str, Any]:
        """
        Get masked version of sensitive data for display

        Returns:
            Dictionary with masked sensitive data
        """

        def mask_email(email: str) -> str:
            if not email or "@" not in email:
                return email
            local, domain = email.split("@", 1)
            if len(local) <= 2:
                return f"{'*' * len(local)}@{domain}"
            return f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}@{domain}"

        def mask_phone(phone: str) -> str:
            if not phone or len(phone) < 4:
                return phone
            return f"{'*' * (len(phone) - 4)}{phone[-4:]}"

        def mask_ssn(ssn: str) -> str:
            if not ssn or len(ssn) != 9:
                return ssn
            return f"***-**-{ssn[-4:]}"

        def mask_address(address: dict) -> str:
            if not address or "street" not in address:
                return str(address)
            street = address["street"]
            city = address.get("city", "")
            state = address.get("state", "")
            return f"*** {city}, {state}"

        masked_data = {}

        # Apply masking to sensitive fields
        if self.email_personal:
            masked_data["email_personal"] = mask_email(self.email_personal)

        if self.phone_mobile:
            masked_data["phone_mobile"] = mask_phone(self.phone_mobile)

        if self.phone_work:
            masked_data["phone_work"] = mask_phone(self.phone_work)

        if self.ssn:
            masked_data["ssn"] = mask_ssn(self.ssn)

        if self.address_home and isinstance(self.address_home, dict):
            masked_data["address_home"] = mask_address(self.address_home)

        if self.address_work and isinstance(self.address_work, dict):
            masked_data["address_work"] = mask_address(self.address_work)

        # Non-sensitive fields can be returned as-is
        non_sensitive_fields = ["first_name", "last_name", "job_title", "department"]
        for field in non_sensitive_fields:
            if hasattr(self, field):
                value = getattr(self, field)
                if value:
                    masked_data[field] = value

        return masked_data


class EncryptedOrganizationProfile(Base):
    """
    Encrypted organization profile for sensitive business information.
    """

    __tablename__ = "encrypted_organization_profiles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    # Business information (encrypted)
    legal_business_name = Column(EncryptedString)
    dba_name = Column(EncryptedString)  # Doing Business As
    tax_id = Column(EncryptedString)  # EIN/Tax ID
    duns_number = Column(EncryptedString)  # DUNS number

    # Contact information (encrypted)
    billing_address = Column(EncryptedAddress)
    shipping_address = Column(EncryptedAddress)
    billing_phone = Column(EncryptedPhone)
    billing_email = Column(EncryptedEmail)

    # Financial information (encrypted)
    bank_account_number = Column(EncryptedString)
    bank_routing_number = Column(EncryptedString)
    payment_method = Column(EncryptedJSON)

    # Legal information (encrypted)
    legal_contact_name = Column(EncryptedString)
    legal_contact_email = Column(EncryptedEmail)
    legal_contact_phone = Column(EncryptedPhone)

    # Additional encrypted data
    business_notes = Column(EncryptedText)
    custom_attributes = Column(EncryptedJSON)

    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate="now()")
    last_encryption_rotation = Column(DateTime(timezone=True))

    # Relationships
    organization = relationship("Organization", back_populates="encrypted_profile")

    def __repr__(self):
        return f"<EncryptedOrganizationProfile(organization_id={self.organization_id})>"

    def get_decrypted_data(self, fields: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Get decrypted organization data for specified fields

        Args:
            fields: List of field names to decrypt (all if None)

        Returns:
            Dictionary with decrypted data
        """
        if fields is None:
            fields = [
                "legal_business_name",
                "dba_name",
                "tax_id",
                "duns_number",
                "billing_address",
                "shipping_address",
                "billing_phone",
                "billing_email",
                "bank_account_number",
                "bank_routing_number",
                "payment_method",
                "legal_contact_name",
                "legal_contact_email",
                "legal_contact_phone",
                "business_notes",
                "custom_attributes",
            ]

        result = {}
        for field in fields:
            if hasattr(self, field):
                value = getattr(self, field)
                result[field] = value

        return result

    def mask_sensitive_fields(self) -> Dict[str, Any]:
        """
        Get masked version of sensitive business data

        Returns:
            Dictionary with masked sensitive data
        """

        def mask_tax_id(tax_id: str) -> str:
            if not tax_id or len(tax_id) < 4:
                return tax_id
            return f"**-**-{tax_id[-4:]}"

        def mask_bank_account(account: str) -> str:
            if not account or len(account) < 4:
                return account
            return f"{'*' * (len(account) - 4)}{account[-4:]}"

        masked_data = {}

        if self.tax_id:
            masked_data["tax_id"] = mask_tax_id(self.tax_id)

        if self.bank_account_number:
            masked_data["bank_account_number"] = mask_bank_account(
                self.bank_account_number
            )

        if self.legal_business_name:
            masked_data["legal_business_name"] = self.legal_business_name

        if self.dba_name:
            masked_data["dba_name"] = self.dba_name

        return masked_data


class EncryptionAuditLog(Base):
    """
    Audit log for encryption operations and key rotations.
    """

    __tablename__ = "encryption_audit_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Operation details
    operation_type = Column(
        String(50), nullable=False
    )  # ENCRYPT, DECRYPT, ROTATE_KEY, etc.
    resource_type = Column(
        String(50), nullable=False
    )  # USER_PROFILE, ORG_PROFILE, etc.
    resource_id = Column(UUID(as_uuid=True), nullable=False)

    # Key information
    key_id = Column(String(64))
    key_version = Column(Integer)

    # User and context
    performed_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"))

    # Operation details (encrypted)
    operation_details = Column(EncryptedJSON)

    # Metadata
    ip_address = Column(String(45))
    user_agent = Column(Text)
    success = Column(Boolean, default=True)
    error_message = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    user = relationship("User")
    organization = relationship("Organization")

    def __repr__(self):
        return f"<EncryptionAuditLog(operation={self.operation_type}, resource={self.resource_type}:{self.resource_id})>"
