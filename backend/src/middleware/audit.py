"""
Audit middleware for comprehensive request/response logging
Automatically logs all HTTP requests, responses, and security events
"""

import json
import logging
import time
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, Request, Response, status
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import StreamingResponse

from src.exceptions.analytics_exceptions import PermissionDeniedException
from src.middleware.multi_tenancy import get_current_tenant_id, get_current_user_id
from src.services.security.audit_service import (
    AuditEventType,
    AuditService,
    AuditSeverity,
)

logger = logging.getLogger(__name__)


class AuditMiddleware(BaseHTTPMiddleware):
    """Middleware to automatically audit all HTTP requests and responses"""

    def __init__(self, app, audit_service: Optional[AuditService] = None):
        super().__init__(app)
        self.audit_service = audit_service or AuditService()

        # Skip audit for these paths to reduce noise
        self.skip_paths = {
            "/health",
            "/docs",
            "/redoc",
            "/openapi.json",
            "/favicon.ico",
            "/static",
        }

        # Sensitive paths to mask in logs
        self.sensitive_paths = {"/auth/login", "/auth/refresh", "/auth/change-password"}

        # Sensitive headers to mask
        self.sensitive_headers = {
            "authorization",
            "cookie",
            "x-api-key",
            "x-cli-poll-token",
            "password",
        }

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request and log audit event"""
        start_time = time.time()

        # Skip audit for certain paths
        if self._should_skip_audit(request):
            response = await call_next(request)
            return response

        # Extract request information
        request_info = await self._extract_request_info(request)

        try:
            # Process the request
            response = await call_next(request)

            # Calculate response time
            response_time = round(
                (time.time() - start_time) * 1000, 2
            )  # in milliseconds

            # Extract response information
            response_info = await self._extract_response_info(response, response_time)

            # Log the audit event
            await self._log_request_response(request_info, response_info)

            return response

        except Exception as e:
            # Log the error
            response_time = round((time.time() - start_time) * 1000, 2)
            await self._log_error(request_info, str(e), response_time)
            raise

    def _should_skip_audit(self, request: Request) -> bool:
        """Check if audit should be skipped for this request"""
        path = request.url.path
        return any(path.startswith(skip_path) for skip_path in self.skip_paths)

    def _is_sensitive_path(self, request: Request) -> bool:
        """Check if this is a sensitive path"""
        path = request.url.path
        return any(
            path.startswith(sensitive_path) for sensitive_path in self.sensitive_paths
        )

    async def _extract_request_info(self, request: Request) -> Dict[str, Any]:
        """Extract relevant information from request"""
        # Basic request info
        request_info = {
            "method": request.method,
            "url": str(request.url),
            "path": request.url.path,
            "query_params": dict(request.query_params),
            "client_ip": self._get_client_ip(request),
            "user_agent": request.headers.get("user-agent"),
        }

        # Get user and organization context
        try:
            request_info["user_id"] = get_current_user_id()
            request_info["organization_id"] = get_current_tenant_id()
        except Exception:
            request_info["user_id"] = None
            request_info["organization_id"] = None

        # Get headers (mask sensitive ones)
        request_info["headers"] = self._mask_sensitive_headers(dict(request.headers))

        # Get request body for certain methods
        if request.method in ["POST", "PUT", "PATCH"]:
            try:
                # For sensitive paths, don't log the body
                if self._is_sensitive_path(request):
                    request_info["body"] = "[REDACTED - Sensitive data]"
                else:
                    body = await request.body()
                    if body:
                        # Try to parse as JSON, otherwise store as string
                        try:
                            request_info["body"] = json.loads(body.decode("utf-8"))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            # If not JSON and not too large, store as string
                            if len(body) <= 1000:  # 1KB limit
                                request_info["body"] = body.decode(
                                    "utf-8", errors="ignore"
                                )
                            else:
                                request_info["body"] = (
                                    f"[Body too large: {len(body)} bytes]"
                                )
                    else:
                        request_info["body"] = None
            except Exception as e:
                logger.warning(f"Failed to extract request body: {e}")
                request_info["body"] = "[Failed to extract]"

        return request_info

    async def _extract_response_info(
        self, response: Response, response_time: float
    ) -> Dict[str, Any]:
        """Extract relevant information from response"""
        response_info = {
            "status_code": response.status_code,
            "response_time_ms": response_time,
            "headers": dict(response.headers),
        }

        # Get content length if available
        if hasattr(response, "headers") and "content-length" in response.headers:
            response_info["content_length"] = response.headers["content-length"]

        return response_info

    def _get_client_ip(self, request: Request) -> Optional[str]:
        """Get client IP address from request"""
        # Check for forwarded IP first
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            return forwarded_for.split(",")[-1].strip()

        # Check for real IP
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip

        # Fall back to client IP
        if hasattr(request, "client") and request.client:
            return request.client.host

        return None

    def _mask_sensitive_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        """Mask sensitive headers in logs"""
        masked_headers = {}
        for key, value in headers.items():
            if key.lower() in self.sensitive_headers:
                masked_headers[key] = "[REDACTED]"
            else:
                masked_headers[key] = value
        return masked_headers

    async def _log_request_response(
        self, request_info: Dict[str, Any], response_info: Dict[str, Any]
    ):
        """Log successful request/response"""
        try:
            # Determine event type based on path and method
            event_type = self._determine_event_type(request_info, response_info)

            # Determine severity based on status code and path
            severity = self._determine_severity(
                response_info["status_code"], request_info["path"]
            )

            # Create action description
            action = f"{request_info['method']} {request_info['path']}"

            # Prepare details
            details = {
                "request": {
                    "method": request_info["method"],
                    "path": request_info["path"],
                    "query_params": request_info["query_params"],
                    "headers": request_info["headers"],
                    "body": request_info.get("body"),
                },
                "response": {
                    "status_code": response_info["status_code"],
                    "response_time_ms": response_info["response_time_ms"],
                    "content_length": response_info.get("content_length"),
                },
            }

            # Determine if request was successful
            success = 200 <= response_info["status_code"] < 400

            # Create resource information
            resource_type, resource_id = self._extract_resource_info(
                request_info["path"]
            )

            # Log the audit event
            with self.audit_service as audit:
                audit.log_event(
                    event_type=event_type,
                    action=action,
                    user_id=request_info.get("user_id"),
                    organization_id=request_info.get("organization_id"),
                    resource_type=resource_type,
                    resource_id=resource_id,
                    details=details,
                    success=success,
                    severity=severity,
                    ip_address=request_info.get("client_ip"),
                    user_agent=request_info.get("user_agent"),
                    endpoint=request_info["path"],
                    http_method=request_info["method"],
                )

        except Exception as e:
            logger.error(f"Failed to log audit event: {e}")

    async def _log_error(
        self, request_info: Dict[str, Any], error_message: str, response_time: float
    ):
        """Log error event"""
        try:
            event_type = AuditEventType.ERROR_EVENT
            severity = AuditSeverity.HIGH

            action = f"{request_info['method']} {request_info['path']} - ERROR"

            details = {
                "request": {
                    "method": request_info["method"],
                    "path": request_info["path"],
                    "query_params": request_info["query_params"],
                    "headers": request_info["headers"],
                    "body": request_info.get("body"),
                },
                "error": {"message": error_message, "response_time_ms": response_time},
            }

            resource_type, resource_id = self._extract_resource_info(
                request_info["path"]
            )

            with self.audit_service as audit:
                audit.log_event(
                    event_type=event_type,
                    action=action,
                    user_id=request_info.get("user_id"),
                    organization_id=request_info.get("organization_id"),
                    resource_type=resource_type,
                    resource_id=resource_id,
                    details=details,
                    success=False,
                    error_message=error_message,
                    severity=severity,
                    ip_address=request_info.get("client_ip"),
                    user_agent=request_info.get("user_agent"),
                    endpoint=request_info["path"],
                    http_method=request_info["method"],
                )

        except Exception as e:
            logger.error(f"Failed to log error audit event: {e}")

    def _determine_event_type(
        self, request_info: Dict[str, Any], response_info: Dict[str, Any]
    ) -> str:
        """Determine audit event type based on request"""
        path = request_info["path"]
        method = request_info["method"]

        # Authentication events
        if "/auth/login" in path:
            if response_info["status_code"] == 200:
                return AuditEventType.USER_LOGIN
            else:
                return AuditEventType.USER_LOGIN_FAILED
        elif "/auth/logout" in path:
            return AuditEventType.USER_LOGOUT
        elif "/auth/register" in path:
            return AuditEventType.USER_REGISTER

        # Document events
        elif "/documents" in path:
            if method == "GET":
                return AuditEventType.DOCUMENT_ACCESS
            elif method == "POST":
                return AuditEventType.DOCUMENT_CREATE
            elif method == "PUT":
                return AuditEventType.DOCUMENT_UPDATE
            elif method == "DELETE":
                return AuditEventType.DOCUMENT_DELETE
            elif method == "GET" and "/download" in path:
                return AuditEventType.DOCUMENT_DOWNLOAD

        # Search events
        elif "/search" in path:
            if method == "POST":
                return AuditEventType.SEARCH_QUERY
            elif method == "GET" and "/suggestions" in path:
                return AuditEventType.SEARCH_QUERY

        # Role/permission events
        elif "/roles" in path or "/permissions" in path:
            if method == "POST":
                return AuditEventType.ROLE_ASSIGNED
            elif method == "DELETE":
                return AuditEventType.ROLE_REVOKED

        # Data export events
        elif "/export" in path:
            return AuditEventType.DATA_EXPORT

        # System events
        elif "/admin" in path or "/system" in path:
            return AuditEventType.SYSTEM_CONFIG_CHANGE

        # Default to generic access event
        return AuditEventType.DOCUMENT_ACCESS

    def _determine_severity(self, status_code: int, path: str) -> str:
        """Determine severity based on status code and path"""
        # Error status codes
        if status_code >= 500:
            return AuditSeverity.CRITICAL
        elif status_code >= 400:
            # Authentication failures are high severity
            if "/auth" in path and status_code == 401:
                return AuditSeverity.HIGH
            # Permission denied is medium-high severity
            elif status_code == 403:
                return AuditSeverity.MEDIUM
            else:
                return AuditSeverity.LOW

        # Sensitive paths are medium severity
        elif any(sensitive in path for sensitive in ["/auth", "/admin", "/roles"]):
            return AuditSeverity.MEDIUM

        # Everything else is low severity
        return AuditSeverity.LOW

    def _extract_resource_info(self, path: str) -> tuple[Optional[str], Optional[str]]:
        """Extract resource type and ID from path"""
        # Document resources
        if "/documents/" in path:
            parts = path.split("/")
            try:
                doc_index = parts.index("documents")
                if doc_index + 1 < len(parts):
                    return "document", parts[doc_index + 1]
            except (ValueError, IndexError):
                pass
            return "document", None

        # User resources
        elif "/users/" in path:
            parts = path.split("/")
            try:
                user_index = parts.index("users")
                if user_index + 1 < len(parts):
                    return "user", parts[user_index + 1]
            except (ValueError, IndexError):
                pass
            return "user", None

        # Organization resources
        elif "/organizations/" in path:
            parts = path.split("/")
            try:
                org_index = parts.index("organizations")
                if org_index + 1 < len(parts):
                    return "organization", parts[org_index + 1]
            except (ValueError, IndexError):
                pass
            return "organization", None

        return None, None


class SecurityEventLogger:
    """Helper class for logging security-related events"""

    def __init__(self, audit_service: Optional[AuditService] = None):
        self.audit_service = audit_service or AuditService()

    def log_access_denied(
        self,
        user_id: str,
        organization_id: str,
        resource: str,
        ip_address: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        """Log access denied event"""
        with self.audit_service as audit:
            audit.log_security_event(
                event_type=AuditEventType.ACCESS_VIOLATION,
                description=f"Access denied to {resource}",
                user_id=user_id,
                organization_id=organization_id,
                severity=AuditSeverity.MEDIUM,
                details=details or {"resource": resource},
                ip_address=ip_address,
            )

    def log_suspicious_activity(
        self,
        description: str,
        organization_id: str,
        user_id: Optional[str] = None,
        ip_address: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        """Log suspicious activity"""
        with self.audit_service as audit:
            audit.log_security_event(
                event_type=AuditEventType.SECURITY_EVENT,
                description=description,
                user_id=user_id,
                organization_id=organization_id,
                severity=AuditSeverity.HIGH,
                details=details,
                ip_address=ip_address,
            )

    def log_privilege_escalation_attempt(
        self,
        user_id: str,
        organization_id: str,
        attempted_action: str,
        ip_address: Optional[str] = None,
    ):
        """Log privilege escalation attempt"""
        with self.audit_service as audit:
            audit.log_security_event(
                event_type=AuditEventType.SECURITY_EVENT,
                description=f"Privilege escalation attempt: {attempted_action}",
                user_id=user_id,
                organization_id=organization_id,
                severity=AuditSeverity.CRITICAL,
                details={"attempted_action": attempted_action},
                ip_address=ip_address,
            )


# Global security event logger instance
security_logger = SecurityEventLogger()
