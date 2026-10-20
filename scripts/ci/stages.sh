#!/usr/bin/env bash
# scripts/ci/stages.sh — Stage function definitions for local CI pipeline runner
# Each function implements one CI pipeline stage

# ─── lint-backend ─────────────────────────────────────────────────────────────

stage_lint_backend() {
    echo "Building lint Docker image..."
    docker build -t rag-lint:ci -f backend/docker/Dockerfile.lint . || return 1

    echo ""
    echo "Running ruff (linting)..."
    docker run --rm rag-lint:ci ruff check backend/src
    local ruff_exit=$?

    # black/isort/mypy have continue-on-error in CI — warn but don't fail
    echo ""
    echo "Running black (formatting check)..."
    if ! docker run --rm rag-lint:ci black --check backend/src; then
        echo -e "${YELLOW}⚠ black check failed (non-blocking)${RESET}"
    fi

    echo ""
    echo "Running isort (import sorting)..."
    if ! docker run --rm rag-lint:ci isort --check-only backend/src; then
        echo -e "${YELLOW}⚠ isort check failed (non-blocking)${RESET}"
    fi

    echo ""
    echo "Running mypy (type checking)..."
    if ! docker run --rm rag-lint:ci mypy backend/src --ignore-missing-imports; then
        echo -e "${YELLOW}⚠ mypy check failed (non-blocking)${RESET}"
    fi

    return $ruff_exit
}

# ─── lint-frontend ────────────────────────────────────────────────────────────

stage_lint_frontend() {
    echo "Installing frontend dependencies..."
    pnpm install --frozen-lockfile --filter multimodal-rag-frontend... || return 1

    echo ""
    echo "Running type-check..."
    pnpm --dir frontend type-check
    local typecheck_exit=$?

    # lint has continue-on-error in CI — warn but don't fail
    echo ""
    echo "Running lint..."
    if ! pnpm --dir frontend lint; then
        echo -e "${YELLOW}⚠ lint check failed (non-blocking)${RESET}"
    fi

    return $typecheck_exit
}

# ─── unit-tests ───────────────────────────────────────────────────────────────

stage_unit_tests() {
    echo "Checking pytest dependencies..."
    pip3 install --quiet pytest pytest-asyncio pytest-cov pytest-xdist 2>/dev/null || \
        pip install --quiet pytest pytest-asyncio pytest-cov pytest-xdist 2>/dev/null

    echo "Installing backend requirements..."
    pip3 install --quiet --build-constraint backend/constraints-ci.txt -r backend/requirements.txt 2>/dev/null || \
        pip install --quiet --build-constraint backend/constraints-ci.txt -r backend/requirements.txt 2>/dev/null

    echo ""
    echo "Running unit tests..."
    ENVIRONMENT=testing DATABASE_URL="sqlite:///:memory:" \
        pytest backend/tests/ \
            -c backend/pytest.ini \
            -m "unit or not (integration or e2e or slow)" \
            --cov=backend/src \
            --cov-report=term-missing \
            -v
    return $?
}

# ─── security-scan ────────────────────────────────────────────────────────────

stage_security_scan() {
    echo "Checking security tools..."
    pip3 install --quiet bandit safety 2>/dev/null || \
        pip install --quiet bandit safety 2>/dev/null

    # Both bandit and safety have continue-on-error in CI — warn but don't fail
    local had_failure=false

    echo ""
    echo "Running bandit (Python security)..."
    if ! bandit -r backend/src -ll -ii -x tests; then
        echo -e "${YELLOW}⚠ bandit found issues (non-blocking)${RESET}"
        had_failure=true
    fi

    echo ""
    echo "Running safety (dependency vulnerabilities)..."
    if ! safety check -r backend/requirements.txt; then
        echo -e "${YELLOW}⚠ safety found vulnerabilities (non-blocking)${RESET}"
        had_failure=true
    fi

    if [[ "$had_failure" == "true" ]]; then
        echo -e "${YELLOW}⚠ Security scan completed with warnings (non-blocking)${RESET}"
    fi

    # Always pass — security scan is continue-on-error in CI
    return 0
}

# ─── frontend-tests ───────────────────────────────────────────────────────────

stage_frontend_tests() {
    echo "Installing frontend dependencies..."
    pnpm install --frozen-lockfile --filter multimodal-rag-frontend... || return 1

    echo ""
    echo "Running frontend tests..."
    CI=true pnpm --dir frontend test:coverage
    local test_exit=$?

    return $test_exit
}

# ─── resilience-tests ─────────────────────────────────────────────────────────

stage_resilience_tests() {
    echo "Checking pytest dependencies..."
    pip3 install --quiet pytest pytest-asyncio pytest-cov 2>/dev/null || \
        pip install --quiet pytest pytest-asyncio pytest-cov 2>/dev/null

    echo "Installing backend requirements..."
    pip3 install --quiet --build-constraint backend/constraints-ci.txt -r backend/requirements.txt 2>/dev/null || \
        pip install --quiet --build-constraint backend/constraints-ci.txt -r backend/requirements.txt 2>/dev/null

    echo ""
    echo "Running resilience tests..."
    set +e
    ENVIRONMENT=testing DATABASE_URL="sqlite:///:memory:" \
        pytest backend/tests/ \
            -c backend/pytest.ini \
            -m "resilience" \
            -v
    local exit_code=$?
    set -e

    # Exit code 5 = no tests collected — treat as success
    if [[ "$exit_code" == "5" ]]; then
        echo "No resilience tests found — skipping"
        return 0
    fi

    return $exit_code
}

# ─── integration-tests ────────────────────────────────────────────────────────

stage_integration_tests() {
    echo "Starting Postgres and Redis via Docker Compose..."
    docker compose -p rag_local_ci -f docker-compose.ci.yml up -d postgres redis || return 1

    echo "Waiting for services to be healthy..."
    local retries=30
    while (( retries > 0 )); do
        if docker compose -p rag_local_ci -f docker-compose.ci.yml ps | grep -q "healthy"; then
            break
        fi
        sleep 2
        (( retries-- ))
    done

    if (( retries == 0 )); then
        echo -e "${RED}Services failed to become healthy${RESET}"
        return 1
    fi

    echo "Installing backend requirements..."
    pip3 install --quiet --build-constraint backend/constraints-ci.txt -r backend/requirements.txt 2>/dev/null || \
        pip install --quiet --build-constraint backend/constraints-ci.txt -r backend/requirements.txt 2>/dev/null

    echo ""
    echo "Running integration tests..."
    set +e
    ENVIRONMENT=testing \
    DATABASE_URL="postgresql://test:test@localhost:5432/test_db" \
    REDIS_URL="redis://localhost:6379/0" \
        pytest backend/tests/integration/ \
            -c backend/pytest.ini \
            -m "integration" \
            -v
    local exit_code=$?
    set -e

    # Exit code 5 = no tests collected — treat as success
    if [[ "$exit_code" == "5" ]]; then
        echo "No integration tests found — skipping"
        return 0
    fi

    return $exit_code
}

# ─── api-contract-tests ───────────────────────────────────────────────────────

stage_api_contract_tests() {
    echo "Starting Postgres via Docker Compose..."
    docker compose -p rag_local_ci -f docker-compose.ci.yml up -d postgres || return 1

    echo "Waiting for Postgres to be healthy..."
    local retries=30
    while (( retries > 0 )); do
        if docker compose -p rag_local_ci -f docker-compose.ci.yml ps postgres | grep -q "healthy"; then
            break
        fi
        sleep 2
        (( retries-- ))
    done

    if (( retries == 0 )); then
        echo -e "${RED}Postgres failed to become healthy${RESET}"
        return 1
    fi

    echo "Installing backend requirements..."
    pip3 install --quiet --build-constraint backend/constraints-ci.txt -r backend/requirements.txt 2>/dev/null || \
        pip install --quiet --build-constraint backend/constraints-ci.txt -r backend/requirements.txt 2>/dev/null

    echo "Installing schemathesis..."
    pip3 install --quiet schemathesis 2>/dev/null || \
        pip install --quiet schemathesis 2>/dev/null

    echo ""
    echo "Running API contract tests..."
    ENVIRONMENT=testing \
    DATABASE_URL="postgresql://test:test@localhost:5432/test_db" \
        pytest backend/tests/api_contract/ \
            -c backend/pytest.ini \
            -v
    return $?
}

# ─── e2e-tests ────────────────────────────────────────────────────────────────

stage_e2e_tests() {
    echo "Building backend image..."
    docker build \
        --build-arg PYTHON_VERSION=3.11 \
        --build-arg TORCH_CPU_ONLY=true \
        -t rag-backend:test \
        -f backend/docker/Dockerfile.prod . || return 1

    echo ""
    echo "Building frontend image..."
    docker build \
        --build-arg BACKEND_URL=http://backend-local-ci:8000 \
        --build-arg NEXT_PUBLIC_SUPABASE_URL="${NEXT_PUBLIC_SUPABASE_URL:-http://localhost:54321}" \
        --build-arg NEXT_PUBLIC_SUPABASE_ANON_KEY="${NEXT_PUBLIC_SUPABASE_ANON_KEY:-ci-test-anon-key}" \
        -t rag-frontend:test \
        -f frontend/Dockerfile.prod . || return 1

    echo ""
    echo "Starting infrastructure services..."
    docker compose -p rag_local_ci -f docker-compose.ci.yml up -d postgres redis || return 1

    echo "Waiting for infrastructure to be healthy..."
    local retries=30
    while (( retries > 0 )); do
        local healthy_count
        healthy_count=$(docker compose -p rag_local_ci -f docker-compose.ci.yml ps | grep -c "healthy" || true)
        if (( healthy_count >= 2 )); then
            break
        fi
        sleep 3
        (( retries-- ))
    done

    if (( retries == 0 )); then
        echo -e "${RED}Infrastructure services failed to become healthy${RESET}"
        return 1
    fi

    echo ""
    echo "Starting backend..."
    docker run -d --name backend-local-ci \
        --network rag_local_ci_ci-network \
        -p 8000:8000 \
        -e DATABASE_URL=postgresql://test:test@postgres:5432/test_db \
        -e REDIS_URL=redis://redis:6379/0 \
        -e ENVIRONMENT=testing \
        -e SECRET_KEY=ci-test-secret-key-32-characters! \
        -e JWT_SECRET_KEY=ci-test-jwt-secret-key-32chars! \
        -e CORS_ORIGINS=http://localhost:3000,http://frontend:3000 \
        -e ENABLE_AI_PROCESSING=false \
        rag-backend:test || return 1

    echo "Starting frontend..."
    docker run -d --name frontend-local-ci \
        --network rag_local_ci_ci-network \
        -p 3000:3000 \
        -e BACKEND_URL=http://backend-local-ci:8000 \
        -e NEXT_PUBLIC_API_URL=http://localhost:8000 \
        rag-frontend:test || return 1

    echo "Waiting for application services..."
    local backend_ready=false
    for i in {1..30}; do
        if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
            echo "Backend is healthy!"
            backend_ready=true
            break
        fi
        sleep 3
    done

    if [[ "$backend_ready" != "true" ]]; then
        echo -e "${RED}Backend failed to start${RESET}"
        echo "=== Backend Logs ==="
        docker logs backend-local-ci --tail=50 2>&1 || true
        return 1
    fi

    local frontend_ready=false
    for i in {1..30}; do
        if curl -sf http://localhost:3000 >/dev/null 2>&1; then
            echo "Frontend is healthy!"
            frontend_ready=true
            break
        fi
        sleep 3
    done

    if [[ "$frontend_ready" != "true" ]]; then
        echo -e "${RED}Frontend failed to start${RESET}"
        echo "=== Frontend Logs ==="
        docker logs frontend-local-ci --tail=50 2>&1 || true
        return 1
    fi

    echo ""
    echo "Running smoke tests..."
    docker compose -p rag_local_ci -f docker-compose.ci.yml --profile smoke run --rm smoke-tests
    return $?
}
