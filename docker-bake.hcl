# docker-bake.hcl - Multi-service Docker builds
# Usage: docker buildx bake -f docker-bake.hcl [target]
# Or: docker buildx bake -f docker-bake.hcl --push
#
# Note: cache configuration is handled by the workflow invoking bake

variable "REGISTRY" {
  default = "267685730035.dkr.ecr.us-east-1.amazonaws.com/nous"
}

variable "TAG" {
  default = "latest"
}

variable "PYTHON_VERSION" {
  default = "3.11"
}

variable "NODE_VERSION" {
  default = "24"
}

# Default group - builds all production services
group "default" {
  targets = ["backend", "frontend"]
}

# All services including specialized workers
group "all" {
  targets = ["backend", "frontend", "worker", "websocket"]
}

# Core services only
group "core" {
  targets = ["backend", "frontend"]
}

# Backend API service
target "backend" {
  dockerfile = "backend/docker/Dockerfile.prod"
  context    = "."
  platforms  = ["linux/amd64", "linux/arm64"]
  tags = [
    "${REGISTRY}/backend:${TAG}",
    "${REGISTRY}/backend:latest"
  ]
  args = {
    PYTHON_VERSION = "${PYTHON_VERSION}"
  }
}

# Frontend Next.js service
target "frontend" {
  dockerfile = "frontend/Dockerfile.prod"
  context    = "."
  platforms  = ["linux/amd64", "linux/arm64"]
  tags = [
    "${REGISTRY}/frontend:${TAG}",
    "${REGISTRY}/frontend:latest"
  ]
  args = {
    NODE_VERSION = "${NODE_VERSION}"
  }
}

# Celery worker service
target "worker" {
  dockerfile = "backend/docker/Dockerfile.worker"
  context    = "."
  platforms  = ["linux/amd64", "linux/arm64"]
  tags = [
    "${REGISTRY}/worker:${TAG}",
    "${REGISTRY}/worker:latest"
  ]
  args = {
    PYTHON_VERSION = "${PYTHON_VERSION}"
  }
}

# WebSocket service
target "websocket" {
  dockerfile = "backend/docker/Dockerfile.websocket.prod"
  context    = "."
  platforms  = ["linux/amd64", "linux/arm64"]
  tags = [
    "${REGISTRY}/websocket:${TAG}",
    "${REGISTRY}/websocket:latest"
  ]
  args = {
    PYTHON_VERSION = "${PYTHON_VERSION}"
  }
}

# Knowledge Graph service
target "knowledge-graph" {
  dockerfile = "backend/docker/Dockerfile.knowledge-graph"
  context    = "."
  platforms  = ["linux/amd64"]
  tags = [
    "${REGISTRY}/knowledge-graph:${TAG}",
    "${REGISTRY}/knowledge-graph:latest"
  ]
}

# Graph Analytics service
target "graph-analytics" {
  dockerfile = "backend/docker/Dockerfile.graph-analytics"
  context    = "."
  platforms  = ["linux/amd64"]
  tags = [
    "${REGISTRY}/graph-analytics:${TAG}",
    "${REGISTRY}/graph-analytics:latest"
  ]
}

# Development build (single platform, no push)
target "backend-dev" {
  inherits   = ["backend"]
  platforms  = ["linux/amd64"]
  dockerfile = "backend/docker/Dockerfile"
  tags       = ["rag-backend:dev"]
  output     = ["type=docker"]
}

target "frontend-dev" {
  inherits   = ["frontend"]
  platforms  = ["linux/amd64"]
  dockerfile = "frontend/Dockerfile"
  target     = "development"
  tags       = ["rag-frontend:dev"]
  output     = ["type=docker"]
}

# Lint tools only (minimal image for CI)
# Build: docker buildx bake -f docker-bake.hcl lint
# Usage: docker run --rm rag-lint:latest ruff check backend/src
target "lint" {
  dockerfile = "backend/docker/Dockerfile.lint"
  context    = "."
  platforms  = ["linux/amd64"]
  tags       = ["rag-lint:latest"]
  output     = ["type=docker"]
  args = {
    PYTHON_VERSION = "${PYTHON_VERSION}"
  }
}
