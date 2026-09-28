"""
Core encryption utilities for data protection at rest and in transit.

This module provides comprehensive encryption capabilities including:
- AES-256-GCM encryption for data at rest
- Field-level encryption for sensitive database columns
- File encryption for document storage
- Key rotation and management
- Secure random generation
"""

import base64
import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple, Union

from cryptography.fernet import Fernet
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, hmac, padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)


class EncryptionError(Exception):
    """Base exception for encryption operations"""

    pass


class KeyManagementError(EncryptionError):
    """Exception for key management operations"""

    pass


class DataEncryptionError(EncryptionError):
    """Exception for data encryption operations"""

    pass


class EncryptionKeyType:
    """Supported encryption key types"""

    MASTER = "master"
    DATA = "data"
    FILE = "file"
    SESSION = "session"


class EncryptionAlgorithm:
    """Supported encryption algorithms"""

    AES256_GCM = "aes256_gcm"
    FERNET = "fernet"
    RSA_OAEP = "rsa_oaep"


class SecureRandomGenerator:
    """Cryptographically secure random number generator"""

    @staticmethod
    def generate_bytes(length: int) -> bytes:
        """Generate cryptographically secure random bytes"""
        return secrets.token_bytes(length)

    @staticmethod
    def generate_hex(length: int) -> str:
        """Generate cryptographically secure hex string"""
        return secrets.token_hex(length)

    @staticmethod
    def generate_url_safe_token(length: int = 32) -> str:
        """Generate URL-safe random token"""
        return secrets.token_urlsafe(length)

    @staticmethod
    def generate_uuid() -> str:
        """Generate UUID4"""
        return secrets.token_hex(16)


class KeyDerivation:
    """Key derivation functions for secure key generation"""

    @staticmethod
    def derive_key_from_password(
        password: str,
        salt: Optional[bytes] = None,
        iterations: int = 100000,
        key_length: int = 32,
    ) -> Tuple[bytes, bytes]:
        """
        Derive encryption key from password using PBKDF2

        Args:
            password: User password
            salt: Optional salt (generated if not provided)
            iterations: Number of PBKDF2 iterations
            key_length: Desired key length in bytes

        Returns:
            Tuple of (derived_key, salt)
        """
        if salt is None:
            salt = SecureRandomGenerator.generate_bytes(16)

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=key_length,
            salt=salt,
            iterations=iterations,
            backend=default_backend(),
        )

        key = kdf.derive(password.encode())
        return key, salt

    @staticmethod
    def derive_key_hkdf(
        input_key_material: bytes,
        salt: Optional[bytes] = None,
        info: Optional[bytes] = None,
        key_length: int = 32,
    ) -> bytes:
        """
        Derive key using HKDF

        Args:
            input_key_material: Input key material
            salt: Optional salt
            info: Optional context info
            key_length: Desired key length in bytes

        Returns:
            Derived key
        """
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=key_length,
            salt=salt,
            info=info,
            backend=default_backend(),
        )

        return hkdf.derive(input_key_material)


class EncryptionKey:
    """Represents an encryption key with metadata"""

    def __init__(
        self,
        key_id: str,
        key_type: str,
        key_data: bytes,
        algorithm: str,
        created_at: datetime,
        expires_at: Optional[datetime] = None,
        is_active: bool = True,
        version: int = 1,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.key_id = key_id
        self.key_type = key_type
        self.key_data = key_data
        self.algorithm = algorithm
        self.created_at = created_at
        self.expires_at = expires_at
        self.is_active = is_active
        self.version = version
        self.metadata = metadata or {}

    def is_expired(self) -> bool:
        """Check if key has expired"""
        if self.expires_at is None:
            return False
        return datetime.utcnow() > self.expires_at

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation"""
        return {
            "key_id": self.key_id,
            "key_type": self.key_type,
            "algorithm": self.algorithm,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "is_active": self.is_active,
            "version": self.version,
            "metadata": self.metadata,
        }


class KeyManager:
    """Manages encryption keys with rotation and secure storage"""

    def __init__(self, master_key_env_var: str = "ENCRYPTION_MASTER_KEY"):
        self.master_key_env_var = master_key_env_var
        self._keys: Dict[str, EncryptionKey] = {}
        self._master_key: Optional[bytes] = None
        self._initialize_master_key()

    def _initialize_master_key(self) -> None:
        """Initialize master key from environment"""
        master_key_b64 = os.getenv(self.master_key_env_var)
        if not master_key_b64:
            raise KeyManagementError(
                f"Master key not found in environment variable {self.master_key_env_var}"
            )

        try:
            self._master_key = base64.b64decode(master_key_b64.encode())
            if len(self._master_key) != 32:
                raise KeyManagementError("Master key must be 32 bytes (256 bits)")
        except Exception as e:
            raise KeyManagementError(f"Invalid master key format: {str(e)}")

    def generate_key(
        self,
        key_type: str,
        algorithm: str = EncryptionAlgorithm.AES256_GCM,
        expires_in_days: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> EncryptionKey:
        """
        Generate a new encryption key

        Args:
            key_type: Type of key to generate
            algorithm: Encryption algorithm to use
            expires_in_days: Optional expiration in days
            metadata: Optional metadata

        Returns:
            Generated encryption key
        """
        key_id = SecureRandomGenerator.generate_hex(16)

        if algorithm == EncryptionAlgorithm.AES256_GCM:
            key_data = SecureRandomGenerator.generate_bytes(32)
        elif algorithm == EncryptionAlgorithm.FERNET:
            key_data = Fernet.generate_key()
        elif algorithm == EncryptionAlgorithm.RSA_OAEP:
            private_key = rsa.generate_private_key(
                public_exponent=65537, key_size=2048, backend=default_backend()
            )
            key_data = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        else:
            raise KeyManagementError(f"Unsupported algorithm: {algorithm}")

        expires_at = None
        if expires_in_days:
            expires_at = datetime.utcnow() + timedelta(days=expires_in_days)

        key = EncryptionKey(
            key_id=key_id,
            key_type=key_type,
            key_data=key_data,
            algorithm=algorithm,
            created_at=datetime.utcnow(),
            expires_at=expires_at,
            metadata=metadata,
        )

        self._keys[key_id] = key
        return key

    def get_key(self, key_id: str) -> Optional[EncryptionKey]:
        """Get encryption key by ID"""
        return self._keys.get(key_id)

    def get_active_key(self, key_type: str) -> Optional[EncryptionKey]:
        """Get active key of specified type"""
        for key in self._keys.values():
            if key.key_type == key_type and key.is_active and not key.is_expired():
                return key
        return None

    def rotate_key(self, key_id: str) -> EncryptionKey:
        """Rotate an existing key"""
        old_key = self.get_key(key_id)
        if not old_key:
            raise KeyManagementError(f"Key not found: {key_id}")

        # Create new key with same type and metadata
        expires_in_days = None
        if old_key.expires_at:
            expires_in_days = (old_key.expires_at - old_key.created_at).days

        new_key = self.generate_key(
            key_type=old_key.key_type,
            algorithm=old_key.algorithm,
            expires_in_days=expires_in_days,
            metadata=old_key.metadata,
        )

        # Deactivate old key
        old_key.is_active = False

        return new_key

    def encrypt_key_data(self, key_data: bytes) -> bytes:
        """Encrypt key data with master key"""
        if not self._master_key:
            raise KeyManagementError("Master key not initialized")

        aesgcm = AESGCM(self._master_key)
        nonce = SecureRandomGenerator.generate_bytes(12)
        encrypted_data = aesgcm.encrypt(nonce, key_data, None)

        # Store nonce with encrypted data
        return nonce + encrypted_data

    def decrypt_key_data(self, encrypted_data: bytes) -> bytes:
        """Decrypt key data with master key"""
        if not self._master_key:
            raise KeyManagementError("Master key not initialized")

        nonce = encrypted_data[:12]
        ciphertext = encrypted_data[12:]

        aesgcm = AESGCM(self._master_key)
        decrypted_data = aesgcm.decrypt(nonce, ciphertext, None)

        return decrypted_data


class AESEncryption:
    """AES-256-GCM encryption for data at rest"""

    def __init__(self, key_manager: KeyManager):
        self.key_manager = key_manager

    def encrypt(
        self,
        data: Union[str, bytes],
        key_id: Optional[str] = None,
        associated_data: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        """
        Encrypt data using AES-256-GCM

        Args:
            data: Data to encrypt
            key_id: Optional key ID (uses active data key if not provided)
            associated_data: Optional associated data for AEAD

        Returns:
            Dictionary with encrypted data and metadata
        """
        if key_id is None:
            key = self.key_manager.get_active_key(EncryptionKeyType.DATA)
            if not key:
                raise DataEncryptionError("No active data encryption key available")
        else:
            key = self.key_manager.get_key(key_id)
            if not key:
                raise DataEncryptionError(f"Key not found: {key_id}")

        if isinstance(data, str):
            data = data.encode("utf-8")

        aesgcm = AESGCM(key.key_data)
        nonce = SecureRandomGenerator.generate_bytes(12)

        encrypted_data = aesgcm.encrypt(nonce, data, associated_data)

        return {
            "encrypted_data": base64.b64encode(encrypted_data).decode("utf-8"),
            "nonce": base64.b64encode(nonce).decode("utf-8"),
            "key_id": key.key_id,
            "algorithm": key.algorithm,
            "associated_data": (
                base64.b64encode(associated_data).decode("utf-8")
                if associated_data
                else None
            ),
        }

    def decrypt(self, encrypted_payload: Dict[str, Any]) -> bytes:
        """
        Decrypt AES-256-GCM encrypted data

        Args:
            encrypted_payload: Dictionary containing encrypted data and metadata

        Returns:
            Decrypted data as bytes
        """
        key_id = encrypted_payload["key_id"]
        key = self.key_manager.get_key(key_id)
        if not key:
            raise DataEncryptionError(f"Key not found: {key_id}")

        encrypted_data = base64.b64decode(encrypted_payload["encrypted_data"])
        nonce = base64.b64decode(encrypted_payload["nonce"])
        associated_data = None
        if encrypted_payload.get("associated_data"):
            associated_data = base64.b64decode(encrypted_payload["associated_data"])

        aesgcm = AESGCM(key.key_data)
        decrypted_data = aesgcm.decrypt(nonce, encrypted_data, associated_data)

        return decrypted_data


class FieldEncryption:
    """Field-level encryption for sensitive database columns"""

    def __init__(self, aes_encryption: AESEncryption):
        self.aes_encryption = aes_encryption

    def encrypt_field(self, value: Any, field_name: str) -> Optional[str]:
        """Encrypt a single field value"""
        if value is None:
            return None

        # Convert to string for encryption
        if not isinstance(value, str):
            value = str(value)

        # Use field name as associated data for additional security
        associated_data = field_name.encode("utf-8")

        encrypted_payload = self.aes_encryption.encrypt(
            value, associated_data=associated_data
        )

        # Store as JSON string in database
        return json.dumps(encrypted_payload)

    def decrypt_field(
        self, encrypted_value: Optional[str], field_name: str
    ) -> Optional[str]:
        """Decrypt a single field value"""
        if encrypted_value is None:
            return None

        try:
            encrypted_payload = json.loads(encrypted_value)
            decrypted_data = self.aes_encryption.decrypt(encrypted_payload)
            return decrypted_data.decode("utf-8")
        except Exception as e:
            logger.error(f"Failed to decrypt field {field_name}: {str(e)}")
            raise DataEncryptionError(f"Field decryption failed: {str(e)}")


class FileEncryption:
    """File encryption for document storage"""

    def __init__(self, key_manager: KeyManager):
        self.key_manager = key_manager
        self.aes_encryption = AESEncryption(key_manager)

    def encrypt_file(
        self, file_data: bytes, original_filename: str, key_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Encrypt file data

        Args:
            file_data: File content to encrypt
            original_filename: Original filename for metadata
            key_id: Optional key ID

        Returns:
            Dictionary with encrypted data and metadata
        """
        if key_id is None:
            key = self.key_manager.get_active_key(EncryptionKeyType.FILE)
            if not key:
                raise DataEncryptionError("No active file encryption key available")
        else:
            key = self.key_manager.get_key(key_id)
            if not key:
                raise DataEncryptionError(f"Key not found: {key_id}")

        # Use filename as associated data
        associated_data = original_filename.encode("utf-8")

        encrypted_payload = self.aes_encryption.encrypt(
            file_data, key_id=key_id, associated_data=associated_data
        )

        # Add file-specific metadata
        encrypted_payload.update(
            {
                "original_filename": original_filename,
                "file_size_bytes": len(file_data),
                "encryption_timestamp": datetime.utcnow().isoformat(),
            }
        )

        return encrypted_payload

    def decrypt_file(self, encrypted_payload: Dict[str, Any]) -> bytes:
        """
        Decrypt file data

        Args:
            encrypted_payload: Dictionary containing encrypted file and metadata

        Returns:
            Decrypted file data
        """
        try:
            return self.aes_encryption.decrypt(encrypted_payload)
        except Exception as e:
            raise DataEncryptionError(f"File decryption failed: {str(e)}")


class HashUtils:
    """Utility functions for secure hashing"""

    @staticmethod
    def hash_password(password: str, salt: Optional[bytes] = None) -> Tuple[str, bytes]:
        """
        Hash password using PBKDF2

        Args:
            password: Password to hash
            salt: Optional salt (generated if not provided)

        Returns:
            Tuple of (hashed_password, salt)
        """
        if salt is None:
            salt = SecureRandomGenerator.generate_bytes(32)

        pwdhash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000)
        return base64.b64encode(pwdhash).decode("utf-8"), salt

    @staticmethod
    def verify_password(password: str, hashed_password: str, salt: bytes) -> bool:
        """
        Verify password against hash

        Args:
            password: Password to verify
            hashed_password: Stored hash
            salt: Salt used for hashing

        Returns:
            True if password matches
        """
        import secrets

        test_hash, _ = HashUtils.hash_password(password, salt)
        return secrets.compare_digest(test_hash, hashed_password)

    @staticmethod
    def hash_data(data: Union[str, bytes], algorithm: str = "sha256") -> str:
        """
        Hash data using specified algorithm

        Args:
            data: Data to hash
            algorithm: Hash algorithm (sha256, sha512, etc.)

        Returns:
            Hex-encoded hash
        """
        if isinstance(data, str):
            data = data.encode("utf-8")

        hash_func = getattr(hashlib, algorithm)()
        hash_func.update(data)
        return hash_func.hexdigest()

    @staticmethod
    def hmac_sign(data: Union[str, bytes], key: bytes) -> str:
        """
        Create HMAC signature

        Args:
            data: Data to sign
            key: Secret key

        Returns:
            Hex-encoded HMAC signature
        """
        if isinstance(data, str):
            data = data.encode("utf-8")

        h = hmac.HMAC(key, hashes.SHA256())
        h.update(data)
        return h.finalize().hex()

    @staticmethod
    def hmac_verify(data: Union[str, bytes], signature: str, key: bytes) -> bool:
        """
        Verify HMAC signature

        Args:
            data: Original data
            signature: HMAC signature to verify
            key: Secret key

        Returns:
            True if signature is valid
        """
        try:
            expected_signature = HashUtils.hmac_sign(data, key)
            return secrets.compare_digest(expected_signature, signature)
        except Exception:
            return False


# Global instances
_key_manager: Optional[KeyManager] = None
_aes_encryption: Optional[AESEncryption] = None
_field_encryption: Optional[FieldEncryption] = None
_file_encryption: Optional[FileEncryption] = None


def initialize_encryption(master_key_env_var: str = "ENCRYPTION_MASTER_KEY") -> None:
    """Initialize global encryption instances"""
    global _key_manager, _aes_encryption, _field_encryption, _file_encryption

    _key_manager = KeyManager(master_key_env_var)
    _aes_encryption = AESEncryption(_key_manager)
    _field_encryption = FieldEncryption(_aes_encryption)
    _file_encryption = FileEncryption(_key_manager)


def get_key_manager() -> KeyManager:
    """Get global key manager instance"""
    if _key_manager is None:
        raise EncryptionError(
            "Encryption not initialized. Call initialize_encryption() first."
        )
    return _key_manager


def get_aes_encryption() -> AESEncryption:
    """Get global AES encryption instance"""
    if _aes_encryption is None:
        raise EncryptionError(
            "Encryption not initialized. Call initialize_encryption() first."
        )
    return _aes_encryption


def get_field_encryption() -> FieldEncryption:
    """Get global field encryption instance"""
    if _field_encryption is None:
        raise EncryptionError(
            "Encryption not initialized. Call initialize_encryption() first."
        )
    return _field_encryption


def get_file_encryption() -> FileEncryption:
    """Get global file encryption instance"""
    if _file_encryption is None:
        raise EncryptionError(
            "Encryption not initialized. Call initialize_encryption() first."
        )
    return _file_encryption


# Convenience functions
def encrypt_sensitive_field(value: Any, field_name: str) -> Optional[str]:
    """Encrypt a sensitive field value"""
    return get_field_encryption().encrypt_field(value, field_name)


def decrypt_sensitive_field(
    encrypted_value: Optional[str], field_name: str
) -> Optional[str]:
    """Decrypt a sensitive field value"""
    return get_field_encryption().decrypt_field(encrypted_value, field_name)


def is_encrypted_payload(value: Any) -> bool:
    """Return True if *value* is a serialized FieldEncryption envelope.

    An encrypted field is stored as ``json.dumps`` of the dict produced by
    ``AESEncryption.encrypt`` (see :meth:`FieldEncryption.encrypt_field`), which
    always carries ``encrypted_data``, ``nonce`` and ``key_id``. Requiring all
    three makes an accidental collision with legitimate plaintext JSON
    (e.g. user-supplied ``EncryptedJSON`` content) negligible.

    Used to tell "stored plaintext, encryption disabled" (safe to return raw on
    a decrypt failure) apart from "stored ciphertext, decryption failed" (must
    NEVER return the raw ciphertext to the caller).
    """
    if not isinstance(value, str):
        return False
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(parsed, dict) and {
        "encrypted_data",
        "nonce",
        "key_id",
    }.issubset(parsed.keys())


def encrypt_file_data(file_data: bytes, filename: str) -> Dict[str, Any]:
    """Encrypt file data"""
    return get_file_encryption().encrypt_file(file_data, filename)


def decrypt_file_data(encrypted_payload: Dict[str, Any]) -> bytes:
    """Decrypt file data"""
    return get_file_encryption().decrypt_file(encrypted_payload)
