# Monitoring Authentication and Authorization Design

> **Stale / historical (2026-10-17):** It is a design document for a subsystem; the described service layout may not match the current code. Verify against `backend/src/` and `backend/openapi.json` before relying on it. Current contracts: [`docs/engineering/`](../../docs/engineering/README.md).

## Overview

This document outlines the comprehensive authentication and authorization strategy for the Multimodal RAG System's monitoring infrastructure. The design ensures secure access to monitoring data while maintaining flexibility for different user roles and service-to-service communication.

## Authentication Strategies

### 1. OAuth 2.0 with Client Credentials Flow

**Use Case**: Service-to-service authentication within the monitoring system

```python
# OAuth 2.0 Client Configuration
MONITORING_CLIENT_CONFIG = {
    "client_id": "monitoring-system-client",
    "client_secret": os.getenv("MONITORING_CLIENT_SECRET"),
    "token_url": "https://auth.multimodal-rag.com/oauth/token",
    "scopes": [
        "monitoring:read",
        "monitoring:write",
        "metrics:collect",
        "alerts:manage",
        "slos:manage"
    ]
}

# Token acquisition for service-to-service calls
async def get_monitoring_token():
    """Get OAuth 2.0 token for monitoring service"""
    async with aiohttp.ClientSession() as session:
        auth = aiohttp.BasicAuth(
            MONITORING_CLIENT_CONFIG["client_id"],
            MONITORING_CLIENT_CONFIG["client_secret"]
        )
        data = {
            "grant_type": "client_credentials",
            "scope": " ".join(MONITORING_CLIENT_CONFIG["scopes"])
        }
        async with session.post(
            MONITORING_CLIENT_CONFIG["token_url"],
            auth=auth,
            data=data
        ) as response:
            token_data = await response.json()
            return token_data["access_token"]
```

### 2. API Key Authentication

**Use Case**: External monitoring tools and third-party integrations

```python
# API Key Management
class MonitoringAPIKey:
    """API Key for monitoring system access"""

    def __init__(self, key_id: str, key_hash: str, permissions: List[str]):
        self.key_id = key_id
        self.key_hash = key_hash
        self.permissions = permissions
        self.created_at = datetime.utcnow()
        self.last_used = None
        self.is_active = True
        self.rate_limit = RateLimit(
            requests_per_minute=100,
            requests_per_hour=1000
        )

# API Key validation
async def validate_api_key(api_key: str, required_permission: str) -> bool:
    """Validate API key and check permissions"""
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    # Check in database
    api_key_record = await db.get_api_key(key_hash)
    if not api_key_record or not api_key_record.is_active:
        return False

    # Check permissions
    if required_permission not in api_key_record.permissions:
        return False

    # Check rate limits
    if not await api_key_record.rate_limit.check_limit():
        return False

    # Update last used
    api_key_record.last_used = datetime.utcnow()
    await db.update_api_key(api_key_record)

    return True
```

### 3. JWT Token Authentication

**Use Case**: User-based access to monitoring dashboards and APIs

```python
# JWT Token Configuration
JWT_CONFIG = {
    "algorithm": "RS256",
    "access_token_expire_minutes": 15,
    "refresh_token_expire_days": 30,
    "issuer": "multimodal-rag-auth",
    "audience": "monitoring-system"
}

# JWT Token generation
def create_monitoring_token(user_id: str, roles: List[str], organization_id: str) -> dict:
    """Create JWT token for monitoring access"""
    now = datetime.utcnow()
    expires = now + timedelta(minutes=JWT_CONFIG["access_token_expire_minutes"])

    payload = {
        "sub": user_id,
        "roles": roles,
        "organization_id": organization_id,
        "scope": "monitoring:read",
        "iat": now,
        "exp": expires,
        "iss": JWT_CONFIG["issuer"],
        "aud": JWT_CONFIG["audience"],
        "jti": str(uuid.uuid4())
    }

    token = jwt.encode(payload, get_private_key(), algorithm=JWT_CONFIG["algorithm"])
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": JWT_CONFIG["access_token_expire_minutes"] * 60
    }

# JWT Token validation
async def validate_jwt_token(token: str, required_permission: str) -> Optional[dict]:
    """Validate JWT token and extract user info"""
    try:
        payload = jwt.decode(
            token,
            get_public_key(),
            algorithms=[JWT_CONFIG["algorithm"]],
            audience=JWT_CONFIG["audience"],
            issuer=JWT_CONFIG["issuer"]
        )

        # Check token expiration
        if datetime.utcnow() > datetime.fromtimestamp(payload["exp"]):
            return None

        # Check permissions based on roles
        if not has_permission(payload["roles"], required_permission):
            return None

        return payload

    except jwt.InvalidTokenError:
        return None
```

## Authorization Framework

### 1. Role-Based Access Control (RBAC)

**Monitoring Roles and Permissions**

```python
from enum import Enum
from typing import Set, Dict, List

class MonitoringRole(Enum):
    SYSTEM_ADMIN = "system_admin"
    ORG_ADMIN = "org_admin"
    MONITORING_ADMIN = "monitoring_admin"
    MONITORING_ANALYST = "monitoring_analyst"
    MONITORING_VIEWER = "monitoring_viewer"
    SERVICE_ACCOUNT = "service_account"

class MonitoringPermission(Enum):
    # Metrics permissions
    METRICS_READ = "metrics:read"
    METRICS_WRITE = "metrics:write"
    METRICS_DELETE = "metrics:delete"

    # SLI/SLO permissions
    SLI_READ = "sli:read"
    SLO_READ = "slo:read"
    SLO_WRITE = "slo:write"
    SLO_DELETE = "slo:delete"

    # Health monitoring permissions
    HEALTH_READ = "health:read"
    HEALTH_WRITE = "health:write"

    # Tracing permissions
    TRACES_READ = "traces:read"
    TRACES_WRITE = "traces:write"

    # Logs permissions
    LOGS_READ = "logs:read"
    LOGS_WRITE = "logs:write"
    LOGS_DELETE = "logs:delete"

    # Alert permissions
    ALERTS_READ = "alerts:read"
    ALERTS_WRITE = "alerts:write"
    ALERTS_ACKNOWLEDGE = "alerts:acknowledge"

    # Dashboard permissions
    DASHBOARDS_READ = "dashboards:read"
    DASHBOARDS_WRITE = "dashboards:write"
    DASHBOARDS_DELETE = "dashboards:delete"
    DASHBOARDS_SHARE = "dashboards:share"

    # System permissions
    SYSTEM_CONFIG = "system:config"
    SYSTEM_ADMIN = "system:admin"

# Role-Permission Mapping
ROLE_PERMISSIONS: Dict[MonitoringRole, Set[MonitoringPermission]] = {
    MonitoringRole.SYSTEM_ADMIN: {
        # Full system access
        *list(MonitoringPermission)
    },

    MonitoringRole.ORG_ADMIN: {
        # Organization-level access
        MonitoringPermission.METRICS_READ,
        MonitoringPermission.METRICS_WRITE,
        MonitoringPermission.SLI_READ,
        MonitoringPermission.SLO_READ,
        MonitoringPermission.SLO_WRITE,
        MonitoringPermission.HEALTH_READ,
        MonitoringPermission.TRACES_READ,
        MonitoringPermission.LOGS_READ,
        MonitoringPermission.ALERTS_READ,
        MonitoringPermission.ALERTS_WRITE,
        MonitoringPermission.ALERTS_ACKNOWLEDGE,
        MonitoringPermission.DASHBOARDS_READ,
        MonitoringPermission.DASHBOARDS_WRITE,
        MonitoringPermission.DASHBOARDS_SHARE,
    },

    MonitoringRole.MONITORING_ADMIN: {
        # Full monitoring management within organization
        MonitoringPermission.METRICS_READ,
        MonitoringPermission.METRICS_WRITE,
        MonitoringPermission.SLI_READ,
        MonitoringPermission.SLO_READ,
        MonitoringPermission.SLO_WRITE,
        MonitoringPermission.HEALTH_READ,
        MonitoringPermission.HEALTH_WRITE,
        MonitoringPermission.TRACES_READ,
        MonitoringPermission.LOGS_READ,
        MonitoringPermission.LOGS_WRITE,
        MonitoringPermission.ALERTS_READ,
        MonitoringPermission.ALERTS_WRITE,
        MonitoringPermission.ALERTS_ACKNOWLEDGE,
        MonitoringPermission.DASHBOARDS_READ,
        MonitoringPermission.DASHBOARDS_WRITE,
        MonitoringPermission.DASHBOARDS_DELETE,
        MonitoringPermission.DASHBOARDS_SHARE,
    },

    MonitoringRole.MONITORING_ANALYST: {
        # Analysis and configuration access
        MonitoringPermission.METRICS_READ,
        MonitoringPermission.SLI_READ,
        MonitoringPermission.SLO_READ,
        MonitoringPermission.SLO_WRITE,
        MonitoringPermission.HEALTH_READ,
        MonitoringPermission.TRACES_READ,
        MonitoringPermission.LOGS_READ,
        MonitoringPermission.ALERTS_READ,
        MonitoringPermission.ALERTS_WRITE,
        MonitoringPermission.DASHBOARDS_READ,
        MonitoringPermission.DASHBOARDS_WRITE,
        MonitoringPermission.DASHBOARDS_SHARE,
    },

    MonitoringRole.MONITORING_VIEWER: {
        # Read-only access
        MonitoringPermission.METRICS_READ,
        MonitoringPermission.SLI_READ,
        MonitoringPermission.SLO_READ,
        MonitoringPermission.HEALTH_READ,
        MonitoringPermission.TRACES_READ,
        MonitoringPermission.LOGS_READ,
        MonitoringPermission.ALERTS_READ,
        MonitoringPermission.DASHBOARDS_READ,
    },

    MonitoringRole.SERVICE_ACCOUNT: {
        # Limited service access
        MonitoringPermission.METRICS_WRITE,
        MonitoringPermission.HEALTH_WRITE,
        MonitoringPermission.LOGS_WRITE,
        MonitoringPermission.TRACES_WRITE,
    }
}
```

### 2. Attribute-Based Access Control (ABAC)

**Fine-grained access control based on attributes**

```python
from dataclasses import dataclass
from typing import Any, Dict, List

@dataclass
class AccessContext:
    user_id: str
    roles: List[str]
    organization_id: str
    permissions: List[str]
    attributes: Dict[str, Any]

class MonitoringABAC:
    """Attribute-Based Access Control for monitoring system"""

    def __init__(self):
        self.policies = [
            OrganizationDataAccessPolicy(),
            ServiceDataAccessPolicy(),
            TimeBasedAccessPolicy(),
            SensitivityBasedAccessPolicy(),
        ]

    async def check_access(
        self,
        context: AccessContext,
        resource: str,
        action: str,
        resource_attributes: Dict[str, Any] = None
    ) -> bool:
        """Check if user has access to resource"""
        for policy in self.policies:
            if not await policy.evaluate(context, resource, action, resource_attributes):
                return False
        return True

class OrganizationDataAccessPolicy:
    """Policy for organization-based data access"""

    async def evaluate(
        self,
        context: AccessContext,
        resource: str,
        action: str,
        resource_attributes: Dict[str, Any]
    ) -> bool:
        # Users can only access their own organization's data
        if resource_attributes and "organization_id" in resource_attributes:
            if context.organization_id != resource_attributes["organization_id"]:
                # System admins can access all organizations
                if "system_admin" not in context.roles:
                    return False
        return True

class ServiceDataAccessPolicy:
    """Policy for service-specific data access"""

    async def evaluate(
        self,
        context: AccessContext,
        resource: str,
        action: str,
        resource_attributes: Dict[str, Any]
    ) -> bool:
        # Service accounts can only access their own service data
        if "service_account" in context.roles:
            if resource_attributes and "service_name" in resource_attributes:
                service_name = context.attributes.get("service_name")
                if service_name != resource_attributes["service_name"]:
                    return False
        return True

class TimeBasedAccessPolicy:
    """Policy for time-based access restrictions"""

    async def evaluate(
        self,
        context: AccessContext,
        resource: str,
        action: str,
        resource_attributes: Dict[str, Any]
    ) -> bool:
        # Example: Restrict access to sensitive operations during maintenance windows
        if action in ["system:config", "metrics:delete", "logs:delete"]:
            now = datetime.utcnow()
            if now.hour >= 2 and now.hour <= 4:  # Maintenance window 2-4 AM UTC
                if "system_admin" not in context.roles:
                    return False
        return True

class SensitivityBasedAccessPolicy:
    """Policy for accessing sensitive monitoring data"""

    async def evaluate(
        self,
        context: AccessContext,
        resource: str,
        action: str,
        resource_attributes: Dict[str, Any]
    ) -> bool:
        # Restrict access to sensitive logs and traces
        if resource in ["logs", "traces"]:
            sensitivity = resource_attributes.get("sensitivity", "public")
            if sensitivity == "sensitive":
                if not any(role in ["system_admin", "org_admin", "monitoring_admin"]
                          for role in context.roles):
                    return False
            elif sensitivity == "confidential":
                if "system_admin" not in context.roles:
                    return False
        return True
```

## Authentication Middleware

### FastAPI Middleware Implementation

```python
from fastapi import HTTPException, Security, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from functools import wraps

security = HTTPBearer(auto_error=False)

class MonitoringAuthMiddleware:
    """Authentication and authorization middleware for monitoring APIs"""

    def __init__(self, abac: MonitoringABAC):
        self.abac = abac

    async def authenticate_request(
        self,
        request: Request,
        credentials: HTTPAuthorizationCredentials = Security(security)
    ) -> AccessContext:
        """Authenticate request and return access context"""

        if not credentials:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Try JWT authentication first
        context = await self._try_jwt_auth(credentials.credentials)
        if context:
            return context

        # Try API key authentication
        context = await self._try_api_key_auth(credentials.credentials)
        if context:
            return context

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    async def _try_jwt_auth(self, token: str) -> Optional[AccessContext]:
        """Try JWT authentication"""
        try:
            payload = await validate_jwt_token(token, "monitoring:read")
            if payload:
                return AccessContext(
                    user_id=payload["sub"],
                    roles=payload["roles"],
                    organization_id=payload["organization_id"],
                    permissions=self._get_permissions_from_roles(payload["roles"]),
                    attributes={}
                )
        except Exception:
            pass
        return None

    async def _try_api_key_auth(self, api_key: str) -> Optional[AccessContext]:
        """Try API key authentication"""
        try:
            key_record = await self._validate_api_key(api_key)
            if key_record:
                return AccessContext(
                    user_id=f"api_key:{key_record.key_id}",
                    roles=["service_account"],
                    organization_id=key_record.organization_id,
                    permissions=key_record.permissions,
                    attributes={"service_name": key_record.service_name}
                )
        except Exception:
            pass
        return None

    def _get_permissions_from_roles(self, roles: List[str]) -> List[str]:
        """Get permissions from user roles"""
        permissions = set()
        for role in roles:
            if role in ROLE_PERMISSIONS:
                permissions.update(ROLE_PERMISSIONS[role])
        return list(permissions)

def require_permission(permission: str):
    """Decorator to require specific permission"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Get access context from request
            request = kwargs.get("request")
            if not request:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Request context not available"
                )

            context = request.state.access_context

            # Check permission
            if permission not in context.permissions:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Insufficient permissions. Required: {permission}"
                )

            # Check ABAC policies
            resource = f"{request.method} {request.url.path}"
            resource_attributes = kwargs.get("resource_attributes", {})

            abac = MonitoringABAC()
            if not await abac.check_access(context, resource, permission, resource_attributes):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied by policy"
                )

            return await func(*args, **kwargs)
        return wrapper
    return decorator
```

## Usage Examples

### API Endpoint with Authentication

```python
from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPBearer

router = APIRouter(prefix="/monitoring/v1")
security = HTTPBearer()
auth_middleware = MonitoringAuthMiddleware(MonitoringABAC())

@router.get("/metrics")
@require_permission("metrics:read")
async def get_metrics(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Security(security)
):
    """Get metrics with authentication and authorization"""

    # Authenticate and get access context
    context = await auth_middleware.authenticate_request(request, credentials)

    # Get organization-specific metrics
    organization_id = context.organization_id
    metrics_data = await metrics_service.get_metrics(
        organization_id=organization_id,
        filters=request.query_params
    )

    return {
        "metrics": metrics_data,
        "access_context": {
            "user_id": context.user_id,
            "organization_id": organization_id,
            "permissions": context.permissions
        }
    }

@router.post("/alerts/rules")
@require_permission("alerts:write")
async def create_alert_rule(
    request: Request,
    alert_rule: AlertRuleDefinition,
    credentials: HTTPAuthorizationCredentials = Security(security)
):
    """Create alert rule with proper authorization"""

    context = await auth_middleware.authenticate_request(request, credentials)

    # Check if user can create alerts for this service
    resource_attributes = {
        "service_name": alert_rule.service_name,
        "organization_id": context.organization_id
    }

    abac = MonitoringABAC()
    if not await abac.check_access(
        context,
        "POST /monitoring/v1/alerts/rules",
        "alerts:write",
        resource_attributes
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot create alerts for this service"
        )

    # Create alert rule
    rule = await alerts_service.create_rule(
        rule_data=alert_rule,
        organization_id=context.organization_id,
        created_by=context.user_id
    )

    return {"rule": rule}
```

### Service-to-Service Authentication

```python
class MonitoringServiceClient:
    """Client for service-to-service monitoring API calls"""

    def __init__(self):
        self.token_cache = {}

    async def make_authenticated_request(
        self,
        method: str,
        endpoint: str,
        data: dict = None
    ):
        """Make authenticated request to monitoring API"""

        # Get or refresh token
        token = await self._get_cached_token()

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        async with aiohttp.ClientSession() as session:
            async with session.request(
                method,
                endpoint,
                headers=headers,
                json=data
            ) as response:
                if response.status == 401:
                    # Token expired, refresh and retry
                    token = await self._refresh_token()
                    headers["Authorization"] = f"Bearer {token}"

                    async with session.request(
                        method,
                        endpoint,
                        headers=headers,
                        json=data
                    ) as retry_response:
                        return await retry_response.json()

                return await response.json()

    async def _get_cached_token(self) -> str:
        """Get cached OAuth token or refresh if needed"""
        # Implementation for token caching and refresh
        pass
```

## Security Best Practices

### 1. Token Security

```python
# Secure token storage
class TokenStore:
    """Secure token storage with encryption"""

    def __init__(self):
        self.encryption_key = os.getenv("TOKEN_ENCRYPTION_KEY")

    async def store_token(self, token_id: str, token_data: dict):
        """Encrypt and store token"""
        encrypted_data = self._encrypt(json.dumps(token_data))
        await redis.setex(
            f"token:{token_id}",
            3600,  # 1 hour expiration
            encrypted_data
        )

    async def get_token(self, token_id: str) -> Optional[dict]:
        """Retrieve and decrypt token"""
        encrypted_data = await redis.get(f"token:{token_id}")
        if encrypted_data:
            decrypted_data = self._decrypt(encrypted_data)
            return json.loads(decrypted_data)
        return None
```

### 2. Rate Limiting

```python
# Rate limiting by user and API key
class MonitoringRateLimiter:
    """Rate limiting for monitoring API endpoints"""

    def __init__(self):
        self.rate_limits = {
            "system_admin": {"requests_per_minute": 1000},
            "org_admin": {"requests_per_minute": 500},
            "monitoring_admin": {"requests_per_minute": 300},
            "monitoring_analyst": {"requests_per_minute": 200},
            "monitoring_viewer": {"requests_per_minute": 100},
            "service_account": {"requests_per_minute": 1000},
        }

    async def check_rate_limit(self, context: AccessContext) -> bool:
        """Check if user has exceeded rate limits"""
        user_role = self._get_highest_priority_role(context.roles)
        limit = self.rate_limits.get(user_role, {"requests_per_minute": 100})

        current_requests = await redis.incr(f"rate_limit:{context.user_id}")
        if current_requests == 1:
            await redis.expire(f"rate_limit:{context.user_id}", 60)

        return current_requests <= limit["requests_per_minute"]
```

### 3. Audit Logging

```python
# Comprehensive audit logging
class MonitoringAuditLogger:
    """Audit logging for monitoring system access"""

    async def log_access_attempt(
        self,
        context: AccessContext,
        resource: str,
        action: str,
        success: bool,
        ip_address: str,
        user_agent: str
    ):
        """Log access attempt for audit purposes"""
        audit_event = {
            "timestamp": datetime.utcnow().isoformat(),
            "user_id": context.user_id,
            "roles": context.roles,
            "organization_id": context.organization_id,
            "resource": resource,
            "action": action,
            "success": success,
            "ip_address": ip_address,
            "user_agent": user_agent,
            "session_id": getattr(context, "session_id", None)
        }

        # Send to audit log system
        await audit_service.log_event(audit_event)
```

This comprehensive authentication and authorization design ensures secure access to the monitoring system while providing the flexibility needed for different types of users and service-to-service communication. The implementation includes proper security measures, audit logging, and fine-grained access controls.