# CI/CD Best Practices for RAG System

> **Status: historical (reconciled 2026-10-17).** This document predates the current workflows; the live CI and release description is [.github/workflows/README.md](../../.github/workflows/README.md). Current deployment: AWS EKS `nous-dev-cluster` (`us-east-1`), namespace `multimodal-rag-system`, Argo CD application `nous-dev-aws`. Qdrant is removed; live retrieval is PostgreSQL full-text. Start with the [AWS deployment overview](aws-overview.md) and [AWS on-call commands](../operations/aws-on-call.md). The text below is kept unedited as a record; do not run its commands without checking them against the current repo.

## Current State Analysis

Your current CI/CD pipeline includes:
- ✅ Linting & formatting checks
- ✅ Automated testing with coverage
- ✅ Docker build validation
- ✅ Security scanning
- ✅ PostgreSQL & Redis services
- ✅ GitHub Actions with hosted runners

## 🎯 Recommended Best Practices

### 1. **Branch Strategy & Workflow**

#### Current Setup
```yaml
on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main, develop]
```

#### ✅ Good Practices
- Use GitFlow: `main` (production), `develop` (staging), feature branches
- **Protect main branch**: Require PR reviews, passing CI checks
- **Branch naming**: `feature/`, `bugfix/`, `hotfix/`, `release/`

#### 🔧 Improvements Needed

```yaml
on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main, develop]
  # Add release workflow
  release:
    types: [published]
  # Add scheduled security scans
  schedule:
    - cron: '0 0 * * 0'  # Weekly on Sunday
```

**Action Items:**
1. Add branch protection rules on GitHub
2. Require 1-2 reviewers for PRs to main
3. Enable "Require status checks to pass" before merging

---

### 2. **Environment Strategy**

#### Current Gap
- No environment-specific deployments
- No staging/production separation

#### ✅ Recommended Structure

```
environments/
├── development (develop branch)
├── staging (main branch, auto-deploy)
└── production (manual approval required)
```

#### 🔧 Implementation

Create separate workflow files:

**`.github/workflows/deploy-staging.yml`**
```yaml
name: Deploy to Staging

on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment:
      name: staging
      url: https://staging.yourapp.com

    steps:
      - uses: actions/checkout@v4
      - name: Deploy to staging
        run: |
          # Deploy to staging environment
          docker-compose -f docker-compose.staging.yml up -d
```

**`.github/workflows/deploy-production.yml`**
```yaml
name: Deploy to Production

on:
  release:
    types: [published]

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment:
      name: production
      url: https://yourapp.com

    steps:
      - uses: actions/checkout@v4
      - name: Deploy to production
        run: |
          # Deploy with approvals required
```

---

### 3. **Testing Strategy**

#### Current Setup
```yaml
test:
  runs-on: ubuntu-latest
  services:
    postgres: ...
    redis: ...
```

#### ✅ Improvements Needed

**A. Test Matrix for Multiple Python Versions**

```yaml
test:
  strategy:
    matrix:
      python-version: ['3.11', '3.12', '3.13']
      os: [ubuntu-latest]

  runs-on: ${{ matrix.os }}

  steps:
    - name: Set up Python ${{ matrix.python-version }}
      uses: actions/setup-python@v5
      with:
        python-version: ${{ matrix.python-version }}
```

**B. Add Integration Tests**

```yaml
integration-test:
  needs: [test]
  runs-on: ubuntu-latest

  services:
    postgres: ...
    redis: ...
    qdrant:
      image: qdrant/qdrant:latest
      ports:
        - 6333:6333
    neo4j:
      image: neo4j:5
      env:
        NEO4J_AUTH: neo4j/testpass
      ports:
        - 7687:7687

  steps:
    - name: Run integration tests
      run: pytest tests/integration/ -v
```

**C. Add E2E Tests**

```yaml
e2e-test:
  needs: [docker-build]
  runs-on: ubuntu-latest

  steps:
    - name: Start services
      run: docker-compose up -d

    - name: Wait for services
      run: |
        timeout 60 bash -c 'until curl -f http://localhost:8000/health; do sleep 2; done'

    - name: Run E2E tests
      run: pytest tests/e2e/ -v
```

**D. Add Performance/Load Tests**

```yaml
performance-test:
  runs-on: ubuntu-latest
  if: github.event_name == 'pull_request'

  steps:
    - name: Run load tests
      run: |
        pip install locust
        locust -f tests/load/locustfile.py --headless -u 100 -r 10 --run-time 5m
```

---

### 4. **Quality Gates**

#### ✅ Current Issues to Fix

**A. Make Linting Fail on Errors**

Current:
```yaml
- name: Check black formatting
  run: black --check src tests || echo "::warning::Code is not formatted with black"
```

**Should be:**
```yaml
- name: Check black formatting
  run: |
    black --check src tests
    if [ $? -ne 0 ]; then
      echo "::error::Code is not formatted with black. Run: black src tests"
      exit 1
    fi
```

**B. Add Coverage Thresholds**

```yaml
- name: Run tests with coverage
  run: |
    pytest tests/ -v \
      --cov=src \
      --cov-report=xml \
      --cov-report=term-missing \
      --cov-fail-under=80  # Fail if coverage < 80%
```

**C. Add Type Checking**

```yaml
- name: Run mypy type checking
  working-directory: backend
  run: |
    mypy src --ignore-missing-imports --strict
```

---

### 5. **Security Best Practices**

#### ✅ Current Setup
- Safety check for vulnerabilities
- Bandit for code security

#### 🔧 Improvements Needed

**A. Add Dependency Scanning**

```yaml
security-scan:
  steps:
    # Add OWASP Dependency Check
    - name: OWASP Dependency Check
      uses: dependency-check/Dependency-Check_Action@main
      with:
        project: 'rag-system'
        path: '.'
        format: 'ALL'

    # Add Trivy for container scanning
    - name: Run Trivy scanner
      uses: aquasecurity/trivy-action@master
      with:
        image-ref: 'rag-backend:test'
        format: 'sarif'
        output: 'trivy-results.sarif'

    - name: Upload Trivy results to GitHub Security
      uses: github/codeql-action/upload-sarif@v2
      with:
        sarif_file: 'trivy-results.sarif'
```

**B. Secret Scanning**

```yaml
- name: TruffleHog Secret Scanning
  uses: trufflesecurity/trufflehog@main
  with:
    path: ./
    base: ${{ github.event.repository.default_branch }}
    head: HEAD
```

**C. Make Security Checks Fail on High Severity**

```yaml
- name: Run safety check
  run: |
    safety check --file requirements.txt --json > safety-report.json
    # Fail on high/critical vulnerabilities
    if jq -e '.vulnerabilities[] | select(.severity == "high" or .severity == "critical")' safety-report.json; then
      echo "::error::High/Critical vulnerabilities found!"
      exit 1
    fi
```

---

### 6. **Docker & Container Best Practices**

#### ✅ Current Setup
- Docker build test
- Multi-stage builds
- Cache optimization

#### 🔧 Improvements Needed

**A. Multi-Architecture Builds**

```yaml
- name: Build multi-arch images
  uses: docker/build-push-action@v5
  with:
    platforms: linux/amd64,linux/arm64
    context: ./backend
    file: ./backend/Dockerfile
    push: false
    tags: rag-backend:test
```

**B. Image Tagging Strategy**

```yaml
- name: Docker metadata
  id: meta
  uses: docker/metadata-action@v5
  with:
    images: ghcr.io/${{ github.repository }}/backend
    tags: |
      type=ref,event=branch
      type=ref,event=pr
      type=semver,pattern={{version}}
      type=semver,pattern={{major}}.{{minor}}
      type=sha,prefix={{branch}}-
```

**C. Push to Registry (for main/releases)**

```yaml
- name: Login to GitHub Container Registry
  if: github.event_name != 'pull_request'
  uses: docker/login-action@v3
  with:
    registry: ghcr.io
    username: ${{ github.actor }}
    password: ${{ secrets.GITHUB_TOKEN }}

- name: Build and push
  if: github.event_name != 'pull_request'
  uses: docker/build-push-action@v5
  with:
    push: true
    tags: ${{ steps.meta.outputs.tags }}
    labels: ${{ steps.meta.outputs.labels }}
```

---

### 7. **Deployment Automation**

#### 🔧 For Your RAG System

**A. Staging Auto-Deploy (on main branch)**

```yaml
deploy-staging:
  needs: [lint, test, docker-build, security-scan]
  if: github.ref == 'refs/heads/main' && github.event_name == 'push'
  runs-on: ubuntu-latest
  environment:
    name: staging

  steps:
    - name: Deploy to staging
      run: |
        # SSH to staging server
        # Pull latest images
        # Run docker-compose up -d
        # Run database migrations
```

**B. Production Deploy (manual approval)**

```yaml
deploy-production:
  needs: [deploy-staging]
  if: github.event_name == 'release'
  runs-on: ubuntu-latest
  environment:
    name: production
    # Requires manual approval in GitHub

  steps:
    - name: Deploy to production
      run: |
        # Similar to staging but for production
```

**C. Database Migration Strategy**

```yaml
- name: Run database migrations
  run: |
    docker-compose exec -T backend alembic upgrade head
```

---

### 8. **Monitoring & Notifications**

#### 🔧 Add Slack/Discord Notifications

```yaml
- name: Notify on failure
  if: failure()
  uses: slackapi/slack-github-action@v1
  with:
    webhook-url: ${{ secrets.SLACK_WEBHOOK }}
    payload: |
      {
        "text": "❌ CI/CD Failed: ${{ github.workflow }}",
        "blocks": [
          {
            "type": "section",
            "text": {
              "type": "mrkdwn",
              "text": "*Workflow:* ${{ github.workflow }}\n*Branch:* ${{ github.ref }}\n*Commit:* ${{ github.sha }}"
            }
          }
        ]
      }
```

#### 🔧 Add Performance Metrics

```yaml
- name: Publish test results
  uses: EnricoMi/publish-unit-test-result-action@v2
  if: always()
  with:
    files: |
      backend/test-results/**/*.xml
```

---

### 9. **Cost Optimization**

#### ✅ Best Practices

**A. Run expensive jobs conditionally**

```yaml
integration-test:
  # Skip on draft PRs
  if: github.event.pull_request.draft == false

performance-test:
  # Only on PR to main
  if: github.base_ref == 'main'

security-scan:
  # Daily schedule + on main branch
  if: github.event_name == 'schedule' || github.ref == 'refs/heads/main'
```

**B. Use job dependencies**

```yaml
jobs:
  lint:
    # Fast, run first

  test:
    needs: [lint]  # Only if lint passes

  docker-build:
    needs: [lint]  # Parallel with test

  deploy:
    needs: [test, docker-build]  # Only if both pass
```

**C. Cache dependencies**

```yaml
- name: Cache pip packages
  uses: actions/cache@v3
  with:
    path: ~/.cache/pip
    key: ${{ runner.os }}-pip-${{ hashFiles('**/requirements*.txt') }}

- name: Cache Docker layers
  uses: actions/cache@v3
  with:
    path: /tmp/.buildx-cache
    key: ${{ runner.os }}-buildx-${{ github.sha }}
    restore-keys: |
      ${{ runner.os }}-buildx-
```

---

### 10. **Documentation & Maintenance**

#### ✅ Add to Your Project

**A. Status Badges in README.md**

```markdown
# RAG System

[![CI/CD](https://github.com/goodwiins/rag/workflows/CI%2FCD%20Pipeline/badge.svg)](https://github.com/goodwiins/rag/actions)
[![Coverage](https://codecov.io/gh/goodwiins/rag/branch/main/graph/badge.svg)](https://codecov.io/gh/goodwiins/rag)
[![Security](https://img.shields.io/badge/security-scanned-green.svg)](https://github.com/goodwiins/rag/security)
```

**B. CHANGELOG.md**

```markdown
# Changelog

## [Unreleased]

## [1.0.0] - 2025-01-15
### Added
- Multi-agent search functionality
- Performance dashboard
```

**C. Version Tagging**

```bash
# Use semantic versioning
git tag -a v1.0.0 -m "Release version 1.0.0"
git push origin v1.0.0
```

---

## 📋 Priority Action Items

### 🔥 High Priority (Do Now)

1. **Fix linting to fail on errors** (currently allows failures)
2. **Add coverage threshold** (--cov-fail-under=80)
3. **Enable branch protection** on main
4. **Add integration tests** for vector DB, knowledge graph
5. **Make security scans fail on critical vulnerabilities**

### ⚡ Medium Priority (Next Sprint)

6. **Add staging environment**
7. **Implement deployment automation**
8. **Add E2E tests**
9. **Set up monitoring/notifications**
10. **Add database migration workflow**

### 📌 Low Priority (Future)

11. **Multi-architecture Docker builds**
12. **Performance/load testing**
13. **Multiple Python version matrix**
14. **Automated changelog generation**

---

## 🛠️ Recommended File Structure

```
.github/
├── workflows/
│   ├── ci.yml                    # Current - linting, testing
│   ├── deploy-staging.yml        # Auto-deploy to staging
│   ├── deploy-production.yml     # Manual deploy to prod
│   ├── security-scan.yml         # Scheduled security scans
│   └── performance-test.yml      # Load testing
├── CODEOWNERS                    # Define code owners
└── dependabot.yml                # Auto dependency updates

docs/
├── CI_CD.md                      # This document
├── DEPLOYMENT.md                 # Deployment guide
└── DEVELOPMENT.md                # Dev setup guide
```

---

## 📊 Metrics to Track

1. **Build Success Rate**: >95%
2. **Test Coverage**: >80%
3. **Security Vulnerabilities**: 0 critical, <5 high
4. **Build Time**: <10 minutes
5. **Deployment Frequency**: Daily (staging), Weekly (production)
6. **Mean Time to Recovery (MTTR)**: <1 hour

---

## 🔗 Additional Resources

- [GitHub Actions Best Practices](https://docs.github.com/en/actions/learn-github-actions/best-practices-for-github-actions)
- [12-Factor App Methodology](https://12factor.net/)
- [Semantic Versioning](https://semver.org/)
- [Conventional Commits](https://www.conventionalcommits.org/)

---

**Last Updated**: 2025-10-09
**Maintainer**: Development Team
