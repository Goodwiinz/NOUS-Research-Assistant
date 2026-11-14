#!/usr/bin/env python3
"""
Test Environment Verification Script
Checks if all required services and dependencies are available for testing
"""

import asyncio
import importlib
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple


# Color codes for output
class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    BOLD = "\033[1m"
    END = "\033[0m"


@dataclass
class ServiceCheck:
    name: str
    status: bool
    message: str
    required: bool = True


class TestEnvironmentVerifier:
    """Verifies test environment setup"""

    def __init__(self):
        self.checks: List[ServiceCheck] = []
        self.start_time = datetime.utcnow()

    def print_header(self):
        """Print verification header"""
        print(f"\n{Colors.BOLD}{Colors.BLUE}{'='*60}")
        print(f"Multi-Agent Search System - Test Environment Verification")
        print(f"{'='*60}{Colors.END}")
        print(f"\nStarted at: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Python version: {sys.version}")
        print(f"Working directory: {os.getcwd()}\n")

    def check_python_packages(self) -> None:
        """Check if required Python packages are installed"""
        print(f"{Colors.BLUE}Checking Python packages...{Colors.END}")

        required_packages = {
            "pytest": "Testing framework",
            "pytest-asyncio": "Async testing support",
            "pytest-cov": "Coverage reporting",
            "fastapi": "Web framework",
            "sqlalchemy": "ORM",
            "pydantic": "Data validation",
            "httpx": "HTTP client",
            "crewai": "Multi-agent framework",
            "deepeval": "RAG evaluation",
            "neo4j": "Graph database driver",
            "redis": "Cache driver",
            "factory_boy": "Test factories",
            "faker": "Fake data generator",
        }

        for package, description in required_packages.items():
            try:
                importlib.import_module(package)
                self.checks.append(
                    ServiceCheck(
                        package,
                        True,
                        f"{description} - {Colors.GREEN}✓ Installed{Colors.END}",
                    )
                )
                print(f"  {Colors.GREEN}✓{Colors.END} {package} - {description}")
            except ImportError:
                status = (
                    "Required"
                    if package in ["pytest", "fastapi", "crewai"]
                    else "Optional"
                )
                color = Colors.RED if status == "Required" else Colors.YELLOW
                self.checks.append(
                    ServiceCheck(
                        package,
                        False,
                        f"{description} - {color}✗ Not installed{Colors.END}",
                    )
                )
                print(f"  {color}✗{Colors.END} {package} - {description} ({status})")

    def check_environment_variables(self) -> None:
        """Check if required environment variables are set"""
        print(f"\n{Colors.BLUE}Checking environment variables...{Colors.END}")

        required_vars = [
            "ENVIRONMENT",
            "DATABASE_URL",
            "REDIS_URL",
            "NEO4J_URI",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
        ]

        for var in required_vars:
            value = os.getenv(var)
            if value:
                # Hide sensitive values
                display_value = value if "KEY" not in var else "***"
                self.checks.append(ServiceCheck(var, True, f"Set to: {display_value}"))
                print(f"  {Colors.GREEN}✓{Colors.END} {var} = {display_value}")
            else:
                self.checks.append(ServiceCheck(var, False, "Not set"))
                print(f"  {Colors.YELLOW}⚠{Colors.END} {var} = Not set")

    async def check_database_connections(self) -> None:
        """Check database connectivity"""
        print(f"\n{Colors.BLUE}Checking database connections...{Colors.END}")

        # Check PostgreSQL
        try:
            import asyncpg
            from test_config import TEST_DATABASES

            conn = await asyncpg.connect(TEST_DATABASES["postgresql"]["url"])
            await conn.execute("SELECT 1")
            await conn.close()
            self.checks.append(
                ServiceCheck("PostgreSQL", True, "Connection successful")
            )
            print(f"  {Colors.GREEN}✓{Colors.END} PostgreSQL - Connection successful")
        except Exception as e:
            self.checks.append(
                ServiceCheck("PostgreSQL", False, f"Connection failed: {str(e)}")
            )
            print(
                f"  {Colors.RED}✗{Colors.END} PostgreSQL - Connection failed: {str(e)}"
            )

        # Check Redis
        try:
            import redis
            from test_config import TEST_REDIS

            r = redis.Redis.from_url(TEST_REDIS["url"])
            r.ping()
            self.checks.append(ServiceCheck("Redis", True, "Connection successful"))
            print(f"  {Colors.GREEN}✓{Colors.END} Redis - Connection successful")
        except Exception as e:
            self.checks.append(
                ServiceCheck("Redis", False, f"Connection failed: {str(e)}")
            )
            print(f"  {Colors.RED}✗{Colors.END} Redis - Connection failed: {str(e)}")

        # Check Neo4j
        try:
            from neo4j import GraphDatabase
            from test_config import TEST_NEO4J

            driver = GraphDatabase.driver(
                TEST_NEO4J["uri"], auth=(TEST_NEO4J["user"], TEST_NEO4J["password"])
            )
            driver.verify_connectivity()
            driver.close()
            self.checks.append(ServiceCheck("Neo4j", True, "Connection successful"))
            print(f"  {Colors.GREEN}✓{Colors.END} Neo4j - Connection successful")
        except Exception as e:
            self.checks.append(
                ServiceCheck("Neo4j", False, f"Connection failed: {str(e)}")
            )
            print(f"  {Colors.RED}✗{Colors.END} Neo4j - Connection failed: {str(e)}")

    def check_docker_services(self) -> None:
        """Check if Docker services are running"""
        print(f"\n{Colors.BLUE}Checking Docker services...{Colors.END}")

        try:
            result = subprocess.run(
                ["docker", "ps", "--format", "table {{.Names}}\t{{.Status}}"],
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                lines = result.stdout.split("\n")[1:]  # Skip header
                services = [line.split("\t")[0] for line in lines if line]

                expected_services = [
                    "neo4j",
                    "rag_system-postgres-1",
                    "rag_system-redis-commander-1",
                ]

                for service in expected_services:
                    running = any(service in s for s in services)
                    if running:
                        self.checks.append(
                            ServiceCheck(f"Docker: {service}", True, "Running")
                        )
                        print(f"  {Colors.GREEN}✓{Colors.END} {service} - Running")
                    else:
                        self.checks.append(
                            ServiceCheck(f"Docker: {service}", False, "Not running")
                        )
                        print(f"  {Colors.RED}✗{Colors.END} {service} - Not running")
            else:
                print(f"  {Colors.YELLOW}⚠{Colors.END} Docker command failed")
        except FileNotFoundError:
            print(f"  {Colors.YELLOW}⚠{Colors.END} Docker not installed")

    def check_file_structure(self) -> None:
        """Check if required files and directories exist"""
        print(f"\n{Colors.BLUE}Checking file structure...{Colors.END}")

        required_items = [
            ("src", "Source code directory"),
            ("src/agents", "Agents directory"),
            ("src/services", "Services directory"),
            ("src/models", "Models directory"),
            ("tests", "Tests directory"),
            ("tests/factories", "Test factories directory"),
            ("test_config.py", "Test configuration file"),
            ("conftest.py", "Pytest configuration file"),
            ("requirements.txt", "Dependencies file"),
            ("requirements-test.txt", "Test dependencies file"),
        ]

        for item, description in required_items:
            path = os.path.join(os.getcwd(), item)
            if os.path.exists(path):
                item_type = "Directory" if os.path.isdir(path) else "File"
                self.checks.append(
                    ServiceCheck(f"File: {item}", True, f"{item_type} exists")
                )
                print(f"  {Colors.GREEN}✓{Colors.END} {item} - {description}")
            else:
                self.checks.append(ServiceCheck(f"File: {item}", False, f"Missing"))
                print(f"  {Colors.RED}✗{Colors.END} {item} - {description} (Missing)")

    def check_test_files(self) -> None:
        """Check if test files exist"""
        print(f"\n{Colors.BLUE}Checking test files...{Colors.END}")

        test_patterns = [
            ("tests/unit", "Unit tests"),
            ("tests/integration", "Integration tests"),
            ("tests/specs", "Specification tests"),
            ("tests/test_*.py", "General tests"),
        ]

        for pattern, description in test_patterns:
            import glob

            files = glob.glob(os.path.join(os.getcwd(), pattern))
            if files:
                count = len(files) if os.path.isdir(files[0]) else 1
                self.checks.append(
                    ServiceCheck(f"Tests: {description}", True, f"{count} files found")
                )
                print(
                    f"  {Colors.GREEN}✓{Colors.END} {description} - {count} files found"
                )
            else:
                self.checks.append(
                    ServiceCheck(f"Tests: {description}", False, "No files found")
                )
                print(f"  {Colors.YELLOW}⚠{Colors.END} {description} - No files found")

    def print_summary(self) -> None:
        """Print verification summary"""
        end_time = datetime.utcnow()
        duration = (end_time - self.start_time).total_seconds()

        total_checks = len(self.checks)
        passed = sum(1 for check in self.checks if check.status)
        failed = total_checks - passed
        critical = sum(
            1 for check in self.checks if not check.status and check.required
        )

        print(f"\n{Colors.BOLD}{'='*60}")
        print(f"VERIFICATION SUMMARY")
        print(f"{'='*60}{Colors.END}")
        print(f"\nDuration: {duration:.2f} seconds")
        print(f"Total checks: {total_checks}")
        print(f"Passed: {Colors.GREEN}{passed}{Colors.END}")
        print(f"Failed: {Colors.RED}{failed}{Colors.END}")
        print(f"Critical failures: {Colors.RED}{critical}{Colors.END}")

        if critical > 0:
            print(f"\n{Colors.RED}CRITICAL ISSUES FOUND:{Colors.END}")
            for check in self.checks:
                if not check.status and check.required:
                    print(f"  • {check.name}: {check.message}")

        # Recommendations
        print(f"\n{Colors.BLUE}RECOMMENDATIONS:{Colors.END}")
        if critical > 0:
            print(f"  • Fix critical issues before running tests")
        if failed > 0:
            print(f"  • Address non-critical issues for better test coverage")
        if passed == total_checks:
            print(f"  {Colors.GREEN}✓ Environment is ready for testing!{Colors.END}")

        # Next steps
        print(f"\n{Colors.BLUE}NEXT STEPS:{Colors.END}")
        print(
            f"  1. Install missing dependencies: pip install -r requirements-test.txt"
        )
        print(f"  2. Start required services: docker-compose up -d")
        print(f"  3. Run tests: pytest")
        print(f"  4. Run specific test suites:")
        print(f"     - Unit tests: pytest tests/unit/")
        print(f"     - Integration tests: pytest tests/integration/")
        print(f"     - Agent tests: pytest -m agent")
        print(f"     - Performance tests: pytest -m performance")

    async def run_verification(self) -> None:
        """Run all verification checks"""
        self.print_header()
        self.check_python_packages()
        self.check_environment_variables()
        await self.check_database_connections()
        self.check_docker_services()
        self.check_file_structure()
        self.check_test_files()
        self.print_summary()


async def main():
    """Main verification function"""
    verifier = TestEnvironmentVerifier()
    await verifier.run_verification()

    # Exit with appropriate code
    critical_failures = sum(
        1 for check in verifier.checks if not check.status and check.required
    )

    if critical_failures > 0:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    # Run verification
    asyncio.run(main())
