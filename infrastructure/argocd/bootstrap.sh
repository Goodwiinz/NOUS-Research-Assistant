#!/usr/bin/env bash
# Bootstrap ArgoCD GitOps for NOUS Platform
# Prerequisites: ArgoCD installed on DO cluster, kubectl configured
#
# Usage:
#   ./infrastructure/argocd/bootstrap.sh
#   ./infrastructure/argocd/bootstrap.sh --dry-run

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=false

if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
  echo "=== DRY RUN MODE ==="
fi

run_cmd() {
  if $DRY_RUN; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

echo "──────────────────────────────────────────────"
echo " NOUS Platform — ArgoCD GitOps Bootstrap"
echo "──────────────────────────────────────────────"
echo ""

# 1. Verify prerequisites
echo "[1/5] Verifying prerequisites..."
command -v kubectl >/dev/null 2>&1 || { echo "ERROR: kubectl not found"; exit 1; }
command -v argocd >/dev/null 2>&1 || echo "WARNING: argocd CLI not found (optional, can use kubectl instead)"

# Check cluster connection
if ! kubectl cluster-info >/dev/null 2>&1; then
  echo "ERROR: Cannot connect to Kubernetes cluster"
  echo "Run: doctl kubernetes cluster kubeconfig save rag-cluster"
  exit 1
fi
echo "  Cluster connected."

# Check ArgoCD namespace
if ! kubectl get namespace argocd >/dev/null 2>&1; then
  echo "ERROR: ArgoCD namespace not found. Is ArgoCD installed?"
  exit 1
fi
echo "  ArgoCD namespace found."

# 2. Create target namespaces
echo ""
echo "[2/5] Creating target namespaces..."
for ns in rag-staging rag-production; do
  if kubectl get namespace "$ns" >/dev/null 2>&1; then
    echo "  Namespace $ns already exists."
  else
    run_cmd kubectl create namespace "$ns"
    echo "  Created namespace $ns."
  fi
done

# 3. Apply ArgoCD Project
echo ""
echo "[3/5] Applying ArgoCD Project..."
run_cmd kubectl apply -f "$SCRIPT_DIR/project.yaml"
echo "  Project 'nous-platform' applied."

# 4. Apply root Application (App of Apps)
echo ""
echo "[4/5] Applying root Application (App of Apps)..."
run_cmd kubectl apply -f "$SCRIPT_DIR/applications/root.yaml"
echo "  Root application 'nous-root' applied."
echo "  This will auto-create staging and production Applications."

# 5. Verify
echo ""
echo "[5/5] Verifying setup..."
if ! $DRY_RUN; then
  echo ""
  echo "  ArgoCD Applications:"
  kubectl get applications -n argocd -l app.kubernetes.io/part-of=nous-platform 2>/dev/null || true
  echo ""
  echo "  Waiting for Applications to appear (up to 30s)..."
  for i in $(seq 1 6); do
    APP_COUNT=$(kubectl get applications -n argocd -l app.kubernetes.io/part-of=nous-platform --no-headers 2>/dev/null | wc -l)
    if [ "$APP_COUNT" -ge 2 ]; then
      echo "  Found $APP_COUNT applications."
      break
    fi
    sleep 5
  done
  kubectl get applications -n argocd --no-headers 2>/dev/null || true
fi

echo ""
echo "──────────────────────────────────────────────"
echo " Bootstrap complete!"
echo "──────────────────────────────────────────────"
echo ""
echo " Staging  (nous-staging):    auto-syncs from 'develop' branch"
echo " Production (nous-production): manual sync from 'main' branch"
echo ""
echo " Next steps:"
echo "   1. Verify in ArgoCD UI or: argocd app list"
echo "   2. Ensure secrets exist in rag-staging and rag-production namespaces:"
echo "      kubectl get secrets -n rag-staging"
echo "      kubectl get secrets -n rag-production"
echo "   3. Push to 'develop' to trigger staging auto-deploy"
echo "   4. This legacy bootstrap has no supported production promotion workflow."
echo "      See .github/workflows/README.md for the current AWS dev release path."
echo ""
