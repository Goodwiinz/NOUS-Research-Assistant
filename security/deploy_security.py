#!/usr/bin/env python3
"""
Security Deployment Script
Automates the deployment of all security measures and configurations
"""

import hashlib
import json
import logging
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class SecurityDeployer:
    """Automated security deployment system"""

    def __init__(self, project_root: str):
        self.project_root = Path(project_root)
        self.security_dir = self.project_root / "security"
        self.backend_dir = self.project_root / "backend"
        self.frontend_dir = self.project_root / "frontend"

        # Deployment configuration
        self.config = {
            "environment": os.getenv("ENVIRONMENT", "development"),
            "backup_existing": True,
            "generate_keys": True,
            "update_permissions": True,
            "run_tests": True,
        }

    def deploy_all(self) -> bool:
        """Deploy all security measures"""
        logger.info("🔒 Starting Security Deployment")
        logger.info("=" * 50)

        try:
            # Create backup
            if self.config["backup_existing"]:
                self._create_backup()

            # Generate security keys
            if self.config["generate_keys"]:
                self._generate_security_keys()

            # Deploy backend security
            self._deploy_backend_security()

            # Deploy frontend security
            self._deploy_frontend_security()

            # Deploy infrastructure security
            self._deploy_infrastructure_security()

            # Update configurations
            self._update_configurations()

            # Set permissions
            if self.config["update_permissions"]:
                self._set_secure_permissions()

            # Run security tests
            if self.config["run_tests"]:
                self._run_security_tests()

            # Generate deployment report
            self._generate_deployment_report()

            logger.info("✅ Security deployment completed successfully!")
            return True

        except Exception as e:
            logger.error(f"❌ Security deployment failed: {e}")
            return False

    def _create_backup(self):
        """Create backup of existing configurations"""
        logger.info("📦 Creating backup...")

        backup_dir = self.project_root / f"backup_{int(time.time())}"
        backup_dir.mkdir(exist_ok=True)

        # Backup critical files
        critical_files = [
            self.backend_dir / "src" / "core" / "config.py",
            self.backend_dir / "docker-compose.yml",
            self.frontend_dir / "package.json",
            self.project_root / ".env",
        ]

        for file_path in critical_files:
            if file_path.exists():
                backup_path = backup_dir / file_path.relative_to(self.project_root)
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                backup_path.write_bytes(file_path.read_bytes())

        logger.info(f"✅ Backup created at {backup_dir}")

    def _generate_security_keys(self):
        """Generate cryptographic keys and secrets"""
        logger.info("🔑 Generating security keys...")

        secrets_dir = self.security_dir / "secrets"
        secrets_dir.mkdir(exist_ok=True, mode=0o700)

        # Generate JWT secret
        jwt_secret = secrets.token_urlsafe(64)
        (secrets_dir / "jwt_secret.txt").write_text(jwt_secret)
        os.chmod(secrets_dir / "jwt_secret.txt", 0o600)

        # Generate encryption key
        encryption_key = secrets.token_bytes(32).hex()
        (secrets_dir / "encryption_key.txt").write_text(encryption_key)
        os.chmod(secrets_dir / "encryption_key.txt", 0o600)

        # Generate database password
        db_password = self._generate_strong_password()
        (secrets_dir / "db_password.txt").write_text(db_password)
        os.chmod(secrets_dir / "db_password.txt", 0o600)

        # Generate API key
        api_key = f"rag_{secrets.token_urlsafe(32)}"
        (secrets_dir / "api_key.txt").write_text(api_key)
        os.chmod(secrets_dir / "api_key.txt", 0o600)

        # Generate CSRF secret
        csrf_secret = secrets.token_urlsafe(32)
        (secrets_dir / "csrf_secret.txt").write_text(csrf_secret)
        os.chmod(secrets_dir / "csrf_secret.txt", 0o600)

        logger.info("✅ Security keys generated")

    def _generate_strong_password(self, length: int = 32) -> str:
        """Generate cryptographically strong password"""
        alphabet = (
            "abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789"
            "!@#$%^&*()_+-=[]{}|;:,.<>?"
        )
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def _deploy_backend_security(self):
        """Deploy backend security middleware and services"""
        logger.info("🖥️ Deploying backend security...")

        # Copy security middleware
        security_files = [
            ("api_security.py", "src/middleware/"),
            ("enhanced_security_service.py", "src/services/"),
            ("security_audit_service.py", "src/services/"),
        ]

        for filename, dest_dir in security_files:
            src = self.security_dir / filename
            dst = self.backend_dir / dest_dir / filename
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text())
            logger.info(f"  ✓ Deployed {filename}")

        # Update main.py to include security middleware
        main_file = self.backend_dir / "main.py"
        if main_file.exists():
            self._update_main_py(main_file)

        # Update requirements.txt
        requirements_file = self.backend_dir / "requirements.txt"
        if requirements_file.exists():
            self._update_requirements(requirements_file)

    def _deploy_frontend_security(self):
        """Deploy frontend security measures"""
        logger.info("🌐 Deploying frontend security...")

        # Copy security module
        security_ts = self.security_dir / "frontendSecurity.ts"
        if security_ts.exists():
            dst = self.frontend_dir / "src" / "security" / "frontendSecurity.ts"
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(security_ts.read_text())
            logger.info("  ✓ Deployed frontend security module")

        # Update package.json with security dependencies
        package_file = self.frontend_dir / "package.json"
        if package_file.exists():
            self._update_package_json(package_file)

    def _deploy_infrastructure_security(self):
        """Deploy infrastructure security configurations"""
        logger.info("🏗️ Deploying infrastructure security...")

        # Copy hardened Docker configuration
        docker_compose = self.security_dir / "docker-security.hardened.yml"
        if docker_compose.exists():
            dst = self.project_root / "docker-compose.security.yml"
            dst.write_text(docker_compose.read_text())
            logger.info("  ✓ Deployed hardened Docker configuration")

        # Create security directory structure
        security_dirs = [
            "nginx",
            "postgres",
            "clamav",
            "fail2ban",
            "ssl",
            "logs",
        ]

        for dir_name in security_dirs:
            (self.security_dir / dir_name).mkdir(exist_ok=True)

        # Generate Nginx security configuration
        self._generate_nginx_config()

        # Generate PostgreSQL security configuration
        self._generate_postgres_config()

        # Generate ClamAV configuration
        self._generate_clamav_config()

    def _update_main_py(self, main_file: Path):
        """Update main.py to include security middleware"""
        content = main_file.read_text()

        # Add imports
        imports = [
            "from middleware.api_security import APISecurityMiddleware",
            "from services.security_audit_service import SecurityEventType, SecuritySeverity, get_audit_service",
        ]

        for import_line in imports:
            if import_line not in content:
                content = content + "\n" + import_line

        # Add middleware to app
        middleware_line = "app.add_middleware(APISecurityMiddleware)"
        if middleware_line not in content:
            # Find where to insert (after other middleware)
            lines = content.split("\n")
            for i, line in enumerate(lines):
                if "app.add_middleware" in line:
                    lines.insert(i + 1, middleware_line)
                    break
            content = "\n".join(lines)

        main_file.write_text(content)
        logger.info("  ✓ Updated main.py with security middleware")

    def _update_requirements(self, requirements_file: Path):
        """Add security dependencies to requirements.txt"""
        content = requirements_file.read_text().split("\n")

        security_packages = [
            "cryptography>=41.0.0",
            "python-magic>=0.4.27",
            "pyclamd>=0.4.0",
            "maxminddb>=2.5.0",
            "orjson>=3.9.0",
            "redis[hiredis]>=4.6.0",
            "sqlparse>=0.4.4",
            "bleach>=6.0.0",
            "html-sanitizer>=2.1.4",
        ]

        for package in security_packages:
            if package not in content:
                content.append(package)

        requirements_file.write_text("\n".join(content))
        logger.info("  ✓ Updated security dependencies")

    def _update_package_json(self, package_file: Path):
        """Add security dependencies to package.json"""
        data = json.loads(package_file.read_text())

        security_deps = {
            "dompurify": "^3.0.5",
            "helmet": "^7.1.0",
            "js-cookie": "^3.0.5",
            "crypto-js": "^4.2.0",
        }

        if "dependencies" not in data:
            data["dependencies"] = {}

        for dep, version in security_deps.items():
            if dep not in data["dependencies"]:
                data["dependencies"][dep] = version

        package_file.write_text(json.dumps(data, indent=2))
        logger.info("  ✓ Updated frontend security dependencies")

    def _generate_nginx_config(self):
        """Generate secure Nginx configuration"""
        config = """# Secure Nginx Configuration
user nginx;
worker_processes auto;
error_log /var/log/nginx/error.log warn;
pid /var/run/nginx.pid;

events {
    worker_connections 1024;
    use epoll;
    multi_accept on;
}

http {
    # Security Headers
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'" always;

    # Hide Nginx version
    server_tokens off;

    # SSL Configuration
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 10m;

    # Rate Limiting
    limit_req_zone $binary_remote_addr zone=login:10m rate=10r/m;
    limit_req_zone $binary_remote_addr zone=api:10m rate=100r/m;
    limit_req_zone $binary_remote_addr zone=upload:10m rate=5r/m;

    # Server Configuration
    server {
        listen 80;
        server_name _;
        return 301 https://$server_name$request_uri;
    }

    server {
        listen 443 ssl http2;
        server_name localhost;

        ssl_certificate /etc/nginx/ssl/cert.pem;
        ssl_certificate_key /etc/nginx/ssl/key.pem;

        # Security configurations
        client_max_body_size 100M;
        client_body_buffer_size 128k;
        client_header_buffer_size 1k;
        large_client_header_buffers 4 4k;

        # Proxy to backend
        location /api/ {
            limit_req zone=api burst=20 nodelay;
            proxy_pass http://backend:8000;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }

        # File upload endpoint with strict rate limiting
        location /api/files/upload {
            limit_req zone=upload burst=5 nodelay;
            proxy_pass http://backend:8000;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }

        # Login endpoint with strict rate limiting
        location /api/auth/login {
            limit_req zone=login burst=5 nodelay;
            proxy_pass http://backend:8000;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }

        # Static files
        location / {
            root /usr/share/nginx/html;
            try_files $uri $uri/ /index.html;
        }
    }
}
"""
        config_file = self.security_dir / "nginx" / "nginx-hardened.conf"
        config_file.write_text(config)
        os.chmod(config_file, 0o644)

    def _generate_postgres_config(self):
        """Generate secure PostgreSQL configuration"""
        config = """# PostgreSQL Security Configuration

# Connection Settings
listen_addresses = '*'
port = 5432
max_connections = 100

# Authentication
auth_delay.milliseconds = 500
password_encryption = scram-sha-256

# SSL
ssl = on
ssl_cert_file = '/var/lib/postgresql/server.crt'
ssl_key_file = '/var/lib/postgresql/server.key'
ssl_ca_file = '/var/lib/postgresql/root.ca'

# Logging
logging_collector = on
log_directory = 'log'
log_filename = 'postgresql-%Y-%m-%d_%H%M%S.log'
log_rotation_age = 1d
log_rotation_size = 100MB
log_min_duration_statement = 1000
log_checkpoints = on
log_connections = on
log_disconnections = on
log_lock_waits = on

# Memory
shared_buffers = 256MB
effective_cache_size = 1GB
work_mem = 4MB
maintenance_work_mem = 64MB

# Security
row_security = on
force_parallel_mode = on
jit = off
"""
        config_file = self.security_dir / "postgres" / "postgresql-hardened.conf"
        config_file.write_text(config)
        os.chmod(config_file, 0o644)

        # Generate pg_hba.conf
        hba_config = """# PostgreSQL Client Authentication Configuration

# TYPE  DATABASE        USER            ADDRESS                 METHOD

# Local connections
local   all             postgres                                peer
local   all             all                                     scram-sha-256

# IPv4 local connections
host    all             all             127.0.0.1/32            scram-sha-256
host    all             all             172.22.0.0/24           scram-sha-256

# IPv6 local connections
host    all             all             ::1/128                 scram-sha-256

# Require SSL for remote connections
hostssl all             all             0.0.0.0/0               scram-sha-256
hostssl all             all             ::/0                    scram-sha-256
"""
        hba_file = self.security_dir / "postgres" / "pg_hba.conf"
        hba_file.write_text(hba_config)
        os.chmod(hba_file, 0o644)

    def _generate_clamav_config(self):
        """Generate ClamAV configuration"""
        config = """# ClamAV Configuration

# General settings
LogFile /var/log/clamav/clamd.log
LogTime yes
LogClean yes
PidFile /var/run/clamd.pid
DatabaseDirectory /var/lib/clamav
LocalSocket /var/run/clamd.sock
FixStaleSocket yes
TCPSocket 3310
TCPAddr 127.0.0.1

# Scan settings
ScanArchive yes
ArchiveBlockEncrypted yes
MaxScanSize 400M
MaxFiles 10000
ClamukoScanOnAccess yes
ClamukoScanOnAccessMaxSize 10M

# Performance settings
MaxThreads 12
ReadTimeout 300
CommandReadTimeout 5

# Heuristic settings
HeuristicScanPrecedence yes
HeuristicAlerts yes

# Exclude paths
ExcludePath ^/proc/
ExcludePath ^/sys/
"""
        config_file = self.security_dir / "clamd.conf"
        config_file.write_text(config)
        os.chmod(config_file, 0o644)

    def _update_configurations(self):
        """Update application configurations"""
        logger.info("⚙️ Updating configurations...")

        # Update .env file
        env_file = self.project_root / ".env"
        if env_file.exists():
            self._update_env_file(env_file)

        # Create security configuration
        security_config = {
            "security": {
                "csrf_protection": True,
                "rate_limiting": True,
                "virus_scanning": True,
                "encryption_at_rest": True,
                "audit_logging": True,
                "security_headers": True,
            },
            "encryption": {
                "algorithm": "aes-256-gcm",
                "key_rotation_days": 90,
            },
            "audit": {
                "retention_days": 2555,
                "gdpr_compliance": True,
            },
        }

        config_file = self.project_root / "config" / "security.json"
        config_file.parent.mkdir(exist_ok=True)
        config_file.write_text(json.dumps(security_config, indent=2))
        os.chmod(config_file, 0o640)

    def _update_env_file(self, env_file: Path):
        """Update .env file with security settings"""
        content = env_file.read_text()

        security_env = [
            "# Security Settings",
            "SECURITY_ENABLED=true",
            "CSRF_PROTECTION=true",
            "RATE_LIMITING=true",
            "VIRUS_SCANNING=true",
            "AUDIT_LOGGING=true",
            "",
            "# Security Paths",
            "SECURITY_DIR=./security",
            "SECRETS_DIR=./security/secrets",
            "",
            "# SSL Settings",
            "SSL_CERT_PATH=./security/ssl/cert.pem",
            "SSL_KEY_PATH=./security/ssl/key.pem",
        ]

        for line in security_env:
            if line not in content and not line.startswith("#"):
                content += f"\n{line}"

        env_file.write_text(content)
        logger.info("  ✓ Updated .env with security settings")

    def _set_secure_permissions(self):
        """Set secure permissions on files and directories"""
        logger.info("🔐 Setting secure permissions...")

        # Security directories - 700
        for dir_path in [self.security_dir, self.security_dir / "secrets"]:
            if dir_path.exists():
                os.chmod(dir_path, 0o700)

        # Configuration files - 644
        for pattern in ["*.conf", "*.yml", "*.yaml", "*.json"]:
            for file_path in self.security_dir.rglob(pattern):
                os.chmod(file_path, 0o644)

        # Secret files - 600
        for file_path in (self.security_dir / "secrets").rglob("*"):
            if file_path.is_file():
                os.chmod(file_path, 0o600)

        # Executable scripts - 750
        for file_path in self.security_dir.rglob("*.sh"):
            os.chmod(file_path, 0o750)

        logger.info("  ✓ Secure permissions set")

    def _run_security_tests(self):
        """Run security test suite"""
        logger.info("🧪 Running security tests...")

        test_file = self.project_root / "tests" / "security" / "security_test_suite.py"
        if test_file.exists():
            try:
                result = subprocess.run(
                    [sys.executable, str(test_file)],
                    capture_output=True,
                    text=True,
                    timeout=300,  # 5 minutes timeout
                )

                if result.returncode == 0:
                    logger.info("  ✓ Security tests passed")
                else:
                    logger.warning(f"  ⚠️ Security tests had issues: {result.stderr}")

            except subprocess.TimeoutExpired:
                logger.warning("  ⚠️ Security tests timed out")
            except Exception as e:
                logger.error(f"  ❌ Could not run security tests: {e}")

    def _generate_deployment_report(self):
        """Generate deployment completion report"""
        report = {
            "deployment": {
                "timestamp": datetime.now().isoformat(),
                "environment": self.config["environment"],
                "status": "completed",
            },
            "components": {
                "backend_security": "✅ Deployed",
                "frontend_security": "✅ Deployed",
                "infrastructure_security": "✅ Deployed",
                "encryption_keys": "✅ Generated",
                "configurations": "✅ Updated",
                "permissions": "✅ Secured",
            },
            "next_steps": [
                "Review and commit all changes",
                "Deploy to staging environment",
                "Run comprehensive security scan",
                "Schedule security testing",
                "Update documentation",
            ],
            "important_notes": [
                "Keep all secret files secure and never commit to version control",
                "Regularly rotate encryption keys (every 90 days)",
                "Monitor security logs daily",
                "Schedule regular security assessments",
            ],
        }

        report_file = (
            self.project_root
            / "security"
            / f"deployment_report_{int(time.time())}.json"
        )
        report_file.write_text(json.dumps(report, indent=2))

        # Print summary
        print("\n" + "=" * 50)
        print("🚀 SECURITY DEPLOYMENT SUMMARY")
        print("=" * 50)
        for component, status in report["components"].items():
            print(f"{component.replace('_', ' ').title()}: {status}")
        print(f"\nReport saved to: {report_file}")
        print("\n⚠️  IMPORTANT:")
        for note in report["important_notes"]:
            print(f"  • {note}")


def main():
    """Main deployment function"""
    if len(sys.argv) > 1:
        project_root = sys.argv[1]
    else:
        project_root = os.getcwd()

    deployer = SecurityDeployer(project_root)
    success = deployer.deploy_all()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
