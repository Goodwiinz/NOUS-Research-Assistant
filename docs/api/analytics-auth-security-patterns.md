# Analytics Dashboard - Authentication and Security Patterns

> **Stale / historical (2026-10-17):** It covers only part of the API and its paths and schemas may have drifted. The source of truth is the generated `backend/openapi.json` (also served at `/docs` when `DEBUG` is on). Current contracts: [`docs/engineering/`](../engineering/README.md).

## 1. Authentication Architecture

### Multi-Layer Authentication Strategy

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Authentication Layer                              │
│                                                                             │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐           │
│  │   API Gateway   │  │  JWT Token      │  │  API Key        │           │
│  │   AuthN/Z       │  │  Service        │  │  Service        │           │
│  │                 │  │                 │  │                 │           │
│  │ - Rate Limiting │  │ - JWT Validation│  │ - Key Rotation  │           │
│  │ - IP Whitelist  │  │ - Token Refresh │  │ - Scope Check   │           │
│  │ - Request Auth  │  │ - Revocation    │  │ - Usage Limits  │           │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘           │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Authorization Layer                                   │
│                                                                             │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐           │
│  │   RBAC Service  │  │  Resource       │  │  Attribute      │           │
│  │                 │  │  Level AuthZ    │  │  Based AuthZ    │           │
│  │ - Role Check    │  │                 │  │                 │           │
│  │ - Permission    │  │ - Resource      │  │ - Context       │           │
│  │   Mapping       │  │   Ownership     │  │   Attributes    │           │
│  │ - Hierarchy     │  │ - Access        │  │ - Dynamic Rules │           │
│  │   Support       │  │   Control Lists │  │ - Time-Based    │           │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘           │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 2. JWT Token Structure

### Access Token Payload
```json
{
  "iss": "analytics-api",
  "sub": "user-uuid",
  "aud": "analytics-dashboard",
  "exp": 1640995200,
  "iat": 1640991600,
  "jti": "token-uuid",
  "org_id": "organization-uuid",
  "user_context": {
    "user_id": "user-uuid",
    "email": "user@company.com",
    "roles": ["analyst", "viewer"],
    "permissions": ["analytics:read", "dashboard:create", "reports:generate"],
    "tenant_id": "tenant-uuid",
    "session_id": "session-uuid"
  },
  "scope": "analytics:read dashboard:manage reports:execute",
  "auth_method": "jwt",
  "mfa_verified": true,
  "client_info": {
    "ip_address": "192.168.1.100",
    "user_agent": "Mozilla/5.0...",
    "client_id": "web-dashboard-v1"
  }
}
```

### Refresh Token Payload
```json
{
  "iss": "analytics-api",
  "sub": "user-uuid",
  "aud": "token-service",
  "exp": 1643587200,
  "iat": 1640991600,
  "jti": "refresh-token-uuid",
  "org_id": "organization-uuid",
  "token_type": "refresh",
  "device_id": "device-uuid",
  "session_persistence": true
}
```

## 3. Role-Based Access Control (RBAC)

### Analytics-Specific Roles

#### Organization Roles
- **super_admin**: Full system access across all organizations
- **org_admin**: Full access within organization scope
- **analytics_admin**: Manage analytics configurations within org
- **data_analyst**: Create and view analytics, manage own dashboards
- **viewer**: Read-only access to shared dashboards and reports

### Permission Matrix

| Permission | Super Admin | Org Admin | Analytics Admin | Data Analyst | Viewer |
|------------|-------------|-----------|-----------------|--------------|--------|
| analytics:read | ✅ | ✅ | ✅ | ✅ | ✅ |
| analytics:write | ✅ | ✅ | ✅ | ✅ | ❌ |
| dashboard:create | ✅ | ✅ | ✅ | ✅ | ❌ |
| dashboard:manage | ✅ | ✅ | ✅ | own | ❌ |
| dashboard:share | ✅ | ✅ | ✅ | own | ❌ |
| reports:create | ✅ | ✅ | ✅ | ✅ | ❌ |
| reports:execute | ✅ | ✅ | ✅ | ✅ | shared |
| reports:schedule | ✅ | ✅ | ✅ | own | ❌ |
| alerts:manage | ✅ | ✅ | ✅ | own | ❌ |
| system:config | ✅ | ❌ | ❌ | ❌ | ❌ |

## 4. API Security Implementation

### Authentication Flow

#### 1. Initial Authentication
```python
# Login with credentials
POST /api/v1/auth/login
{
  "email": "user@company.com",
  "password": "secure_password",
  "mfa_code": "123456",
  "remember_me": true
}

# Response
{
  "access_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
  "refresh_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
  "token_type": "Bearer",
  "expires_in": 3600,
  "user_info": {
    "user_id": "uuid",
    "name": "John Doe",
    "email": "user@company.com",
    "roles": ["data_analyst"],
    "organization": {
      "org_id": "uuid",
      "name": "Acme Corp"
    }
  }
}
```

#### 2. Token Refresh
```python
POST /api/v1/auth/refresh
{
  "refresh_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..."
}
```

#### 3. WebSocket Authentication
```python
# WebSocket connection with token
WS /api/v1/analytics/realtime/subscribe?token=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...

# Or with API key
WS /api/v1/analytics/realtime/subscribe?api_key=ak_live_123456789
```

### Middleware Implementation

#### Authentication Middleware
```python
class AnalyticsAuthMiddleware:
    def __init__(self, public_paths: List[str] = None):
        self.public_paths = public_paths or ["/health", "/metrics"]
        self.jwt_service = JWTService()
        self.api_key_service = APIKeyService()

    async def __call__(self, request: Request, call_next):
        # Skip auth for public paths
        if request.url.path in self.public_paths:
            return await call_next(request)

        # Try JWT authentication first
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
            user_context = await self.jwt_service.validate_token(token)
            if user_context:
                request.state.user = user_context
                request.state.auth_method = "jwt"
                return await call_next(request)

        # Try API key authentication
        api_key = request.headers.get("X-API-Key")
        if api_key:
            key_context = await self.api_key_service.validate_key(api_key)
            if key_context:
                request.state.user = key_context
                request.state.auth_method = "api_key"
                return await call_next(request)

        raise HTTPException(status_code=401, detail="Authentication required")
```

#### Authorization Middleware
```python
class AnalyticsAuthzMiddleware:
    def __init__(self):
        self.rbac_service = RBACService()
        self.resource_service = ResourceService()

    async def __call__(self, request: Request, call_next):
        user = getattr(request.state, "user", None)
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")

        # Extract required permissions from endpoint
        required_permissions = self._extract_permissions(request)
        organization_id = self._extract_organization_id(request)

        # Check organization access
        if organization_id and not await self.rbac_service.check_org_access(
            user["user_id"], organization_id
        ):
            raise HTTPException(status_code=403, detail="Organization access denied")

        # Check permissions
        for permission in required_permissions:
            if not await self.rbac_service.check_permission(
                user["user_id"], permission, organization_id
            ):
                raise HTTPException(
                    status_code=403,
                    detail=f"Permission denied: {permission}"
                )

        # Set context for downstream services
        request.state.context = {
            "user_id": user["user_id"],
            "organization_id": organization_id,
            "roles": user.get("roles", []),
            "permissions": user.get("permissions", [])
        }

        return await call_next(request)
```

## 5. API Key Management

### API Key Types

#### Service Keys
- **Service Integration**: For microservice-to-microservice communication
- **Automated Scripts**: For scheduled jobs and automation
- **Third-party Integrations**: For external system access

#### User Keys
- **Personal Access Tokens**: For individual developer access
- **Session Keys**: Temporary keys for specific operations

### API Key Structure
```json
{
  "key_id": "key_uuid",
  "key_prefix": "ak_live_",
  "key_hash": "sha256_hash",
  "organization_id": "org_uuid",
  "created_by": "user_uuid",
  "key_type": "service_integration",
  "scopes": ["analytics:read", "reports:execute"],
  "rate_limits": {
    "requests_per_minute": 1000,
    "requests_per_hour": 50000
  },
  "ip_restrictions": ["192.168.1.0/24", "10.0.0.0/8"],
  "expires_at": "2024-12-31T23:59:59Z",
  "last_used_at": "2024-01-15T14:30:00Z",
  "is_active": true,
  "created_at": "2024-01-01T00:00:00Z"
}
```

## 6. Security Headers and CORS

### Security Headers Configuration
```python
SecurityHeadersMiddleware = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self' wss://api.company.com"
    ),
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()"
}
```

### CORS Configuration
```python
CORSMiddleware = {
    "allow_origins": [
        "https://dashboard.company.com",
        "https://analytics.company.com",
        "http://localhost:3000"  # Development
    ],
    "allow_credentials": True,
    "allow_methods": ["GET", "POST", "PUT", "DELETE", "PATCH"],
    "allow_headers": [
        "Authorization",
        "X-API-Key",
        "Content-Type",
        "X-Requested-With",
        "X-Organization-ID"
    ],
    "expose_headers": ["X-Total-Count", "X-Rate-Limit-Remaining"],
    "max_age": 86400
}
```

## 7. Rate Limiting Strategy

### Multi-Tier Rate Limiting

#### Global Rate Limits
- **Anonymous Requests**: 100 requests/hour
- **Authenticated Users**: 1000 requests/hour
- **Premium Users**: 5000 requests/hour

#### Endpoint-Specific Limits
```python
RATE_LIMITS = {
    "/api/v1/analytics/realtime/metrics": {
        "authenticated": {"requests_per_minute": 60},
        "premium": {"requests_per_minute": 300}
    },
    "/api/v1/analytics/graph/centrality": {
        "authenticated": {"requests_per_hour": 10},
        "premium": {"requests_per_hour": 100}
    },
    "/api/v1/analytics/reports/execute": {
        "authenticated": {"requests_per_day": 5},
        "premium": {"requests_per_day": 50}
    }
}
```

## 8. Audit Logging

### Security Event Logging
```python
class SecurityEventLogger:
    def __init__(self):
        self.logger = logging.getLogger("security.events")

    async def log_authentication_event(
        self,
        event_type: str,
        user_id: str,
        ip_address: str,
        user_agent: str,
        success: bool,
        details: dict = None
    ):
        event = {
            "timestamp": datetime.utcnow().isoformat(),
            "event_type": event_type,
            "user_id": user_id,
            "ip_address": ip_address,
            "user_agent": user_agent,
            "success": success,
            "details": details or {},
            "organization_id": details.get("organization_id") if details else None
        }
        await self._log_event(event)

    async def log_authorization_event(
        self,
        user_id: str,
        resource: str,
        action: str,
        permission: str,
        granted: bool,
        organization_id: str = None
    ):
        event = {
            "timestamp": datetime.utcnow().isoformat(),
            "event_type": "authorization_check",
            "user_id": user_id,
            "resource": resource,
            "action": action,
            "permission": permission,
            "granted": granted,
            "organization_id": organization_id
        }
        await self._log_event(event)
```

## 9. WebSocket Security

### WebSocket Authentication
```python
class WebSocketAuthenticator:
    def __init__(self):
        self.jwt_service = JWTService()

    async def authenticate_websocket(
        self,
        websocket: WebSocket,
        token: str = None,
        api_key: str = None
    ):
        if token:
            user_context = await self.jwt_service.validate_token(token)
            if user_context:
                await self._setup_authenticated_connection(websocket, user_context)
                return True

        if api_key:
            key_context = await self.api_key_service.validate_key(api_key)
            if key_context:
                await self._setup_authenticated_connection(websocket, key_context)
                return True

        await websocket.close(code=4001, reason="Authentication failed")
        return False

    async def _setup_authenticated_connection(
        self,
        websocket: WebSocket,
        context: dict
    ):
        await websocket.accept()

        # Store connection info
        connection_id = str(uuid.uuid4())
        self.connection_manager.add_connection(
            connection_id,
            websocket,
            context
        )

        # Send confirmation
        await websocket.send_json({
            "type": "connection_established",
            "connection_id": connection_id,
            "user_id": context["user_id"],
            "organization_id": context["organization_id"]
        })
```

## 10. Multi-Tenant Security

### Tenant Isolation Strategies

#### Data Isolation
- **Row Level Security (RLS)**: PostgreSQL policies for tenant data isolation
- **Database Schema Separation**: Separate schemas per tenant (for large tenants)
- **Connection Pooling**: Tenant-specific connection pools

#### Resource Isolation
```python
class TenantSecurityMiddleware:
    async def __call__(self, request: Request, call_next):
        context = getattr(request.state, "context", None)
        if not context:
            raise HTTPException(status_code=401, detail="Authentication required")

        # Set tenant context for database connections
        tenant_id = context["organization_id"]
        set_tenant_context(tenant_id)

        # Validate tenant access to resources
        resource_id = self._extract_resource_id(request)
        if resource_id and not await self._validate_tenant_access(
            tenant_id, resource_id, request.method
        ):
            raise HTTPException(status_code=404, detail="Resource not found")

        response = await call_next(request)

        # Clear tenant context
        clear_tenant_context()

        return response
```

## 11. Implementation Checklist

### Security Implementation Tasks

- [ ] JWT service with token validation and refresh
- [ ] API key management service
- [ ] RBAC service with role and permission management
- [ ] Authentication middleware for API and WebSocket
- [ ] Authorization middleware with permission checking
- [ ] Rate limiting middleware with tiered limits
- [ ] Security headers middleware
- [ ] CORS configuration for analytics domains
- [ ] Audit logging for security events
- [ ] Tenant isolation implementation
- [ ] WebSocket connection management
- [ ] API key rotation and revocation
- [ ] MFA integration for sensitive operations
- [ ] Session management and concurrent session limits

### Configuration Requirements

```python
ANALYTICS_SECURITY_CONFIG = {
    "jwt": {
        "secret_key": os.getenv("JWT_SECRET_KEY"),
        "algorithm": "HS256",
        "access_token_expire_minutes": 30,
        "refresh_token_expire_days": 7,
        "issuer": "analytics-api"
    },
    "api_keys": {
        "prefix": "ak_live_",
        "default_expiry_days": 365,
        "max_keys_per_user": 10
    },
    "rate_limiting": {
        "storage": "redis",
        "default_limits": {
            "anonymous": {"requests_per_hour": 100},
            "user": {"requests_per_hour": 1000},
            "premium": {"requests_per_hour": 5000}
        }
    },
    "websocket": {
        "max_connections_per_user": 10,
        "connection_timeout_minutes": 30,
        "heartbeat_interval_seconds": 30
    }
}
```

This comprehensive security design ensures that the Knowledge Graph Analytics Dashboard maintains strong authentication, authorization, and security controls while supporting multi-tenant architecture and real-time features.