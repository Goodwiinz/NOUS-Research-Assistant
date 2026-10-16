#!/bin/bash

# =============================================================================
# Multimodal RAG System Deployment Script
# =============================================================================

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
NAMESPACE=${NAMESPACE:-"multimodal-rag-system"}
ENVIRONMENT=${ENVIRONMENT:-"staging"}
DEPLOYMENT_STRATEGY=${DEPLOYMENT_STRATEGY:-"rolling-update"}
KUBE_CONFIG=${KUBE_CONFIG:-"${HOME}/.kube/config"}
MONITORING_ENABLED=${MONITORING_ENABLED:-"true"}
SECURITY_SCANNING=${SECURITY_SCANNING:-"true"}

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check prerequisites
check_prerequisites() {
    log_info "Checking prerequisites..."

    # Check if kubectl is installed
    if ! command -v kubectl &> /dev/null; then
        log_error "kubectl is not installed. Please install kubectl first."
        exit 1
    fi

    # Check if Docker is installed
    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed. Please install Docker first."
        exit 1
    fi

    # Check if KUBE_CONFIG file exists
    if [ ! -f "$KUBE_CONFIG" ]; then
        log_error "Kubernetes config file not found at $KUBE_CONFIG"
        exit 1
    fi

    # Check if we have access to the cluster
    if ! kubectl cluster-info &> /dev/null; then
        log_error "Cannot access Kubernetes cluster. Please check your kubeconfig."
        exit 1
    fi

    log_success "All prerequisites are met."
}

# Create namespace if it doesn't exist
create_namespace() {
    log_info "Creating namespace $NAMESPACE..."

    if ! kubectl get namespace "$NAMESPACE" &> /dev/null; then
        kubectl create namespace "$NAMESPACE"
        log_success "Namespace $NAMESPACE created."
    else
        log_warning "Namespace $NAMESPACE already exists."
    fi
}

# Apply Kubernetes manifests
apply_kubernetes_manifests() {
    log_info "Applying Kubernetes manifests..."

    # Apply namespace
    kubectl apply -f kubernetes/namespace.yaml

    # Apply configmaps
    kubectl apply -f kubernetes/configmaps/backend-config.yaml
    # Secrets are materialized by Infisical or scripts/setup-secrets.sh.
    # Do not apply static Secret manifests from git.

    # Apply monitoring if enabled
    if [ "$MONITORING_ENABLED" = "true" ]; then
        log_info "Applying monitoring stack..."
        kubectl apply -f kubernetes/monitoring/
    fi

    # Apply storage classes
    kubectl apply -f kubernetes/storage/

    # Apply base resources
    kubectl apply -f kubernetes/base/

    # Apply environment-specific overlays
    if [ -d "kubernetes/overlays/$ENVIRONMENT" ]; then
        log_info "Applying $ENVIRONMENT overlay..."
        kubectl apply -f kubernetes/overlays/$ENVIRONMENT/
    else
        log_warning "No overlay found for $ENVIRONMENT, using base configuration."
        kubectl apply -f kubernetes/overlays/staging/
    fi

    # Wait for pods to be ready
    log_info "Waiting for pods to be ready..."
    kubectl wait --for=condition=ready pod -l app=backend -n "$NAMESPACE" --timeout=300s
    kubectl wait --for=condition=ready pod -l app=frontend -n "$NAMESPACE" --timeout=300s

    log_success "Kubernetes manifests applied successfully."
}

# Build and push Docker images
build_and_push_images() {
    log_info "Building and pushing Docker images..."

    # Build backend image
    log_info "Building backend image..."
    docker build -f backend/Dockerfile.websocket.prod -t multimodal-rag/backend:latest .
    docker push multimodal-rag/backend:latest

    # Build frontend image
    log_info "Building frontend image..."
    docker build \
        --build-arg BACKEND_URL="${BACKEND_URL:-http://backend:8000}" \
        --build-arg NEXT_PUBLIC_SUPABASE_URL="${NEXT_PUBLIC_SUPABASE_URL:?Set NEXT_PUBLIC_SUPABASE_URL before building}" \
        --build-arg NEXT_PUBLIC_SUPABASE_ANON_KEY="${NEXT_PUBLIC_SUPABASE_ANON_KEY:?Set NEXT_PUBLIC_SUPABASE_ANON_KEY before building}" \
        -f frontend/Dockerfile.prod -t multimodal-rag/frontend:latest .
    docker push multimodal-rag/frontend:latest

    log_success "Docker images built and pushed successfully."
}

# Run security scans
run_security_scans() {
    if [ "$SECURITY_SCANNING" = "true" ]; then
        log_info "Running security scans..."

        # Run Trivy scan
        if command -v trivy &> /dev/null; then
            log_info "Running Trivy vulnerability scan..."
            trivy image multimodal-rag/backend:latest --format json -o trivy-backend.json || true
            trivy image multimodal-rag/frontend:latest --format json -o trivy-frontend.json || true
        else
            log_warning "Trivy is not installed. Skipping vulnerability scan."
        fi

        # Run security check
        if command -v bandit &> /dev/null; then
            log_info "Running Bandit security scan..."
            bandit -r . -f json -o bandit-report.json || true
        else
            log_warning "Bandit is not installed. Skipping security scan."
        fi

        log_success "Security scans completed."
    else
        log_warning "Security scanning is disabled."
    fi
}

# Run health checks
run_health_checks() {
    log_info "Running health checks..."

    # Wait for services to be ready
    kubectl wait --for=condition=ready pod -l app=backend -n "$NAMESPACE" --timeout=300s
    kubectl wait --for=condition=ready pod -l app=frontend -n "$NAMESPACE" --timeout=300s

    # Run health checks
    log_info "Checking backend health..."
    kubectl exec deployment/backend -n "$NAMESPACE" -- curl -f http://localhost:8000/health || {
        log_error "Backend health check failed."
        exit 1
    }

    log_info "Checking frontend health..."
    kubectl exec deployment/frontend -n "$NAMESPACE" -- curl -f http://localhost:3000 || {
        log_error "Frontend health check failed."
        exit 1
    }

    # Check WebSocket connectivity
    log_info "Checking WebSocket connectivity..."
    kubectl exec deployment/backend -n "$NAMESPACE" -- curl -f http://localhost:8001/ws/health || {
        log_error "WebSocket health check failed."
        exit 1
    }

    log_success "All health checks passed."
}

# Run performance tests
run_performance_tests() {
    log_info "Running performance tests..."

    # Test API response times
    log_info "Testing API response times..."
    for i in {1..10}; do
        response_time=$(kubectl exec deployment/backend -n "$NAMESPACE" -- curl -o /dev/null -s -w '%{time_total}' http://localhost:8000/health)
        echo "Request $i: ${response_time}s"
    done

    # Test WebSocket connection
    log_info "Testing WebSocket connection..."
    kubectl exec deployment/backend -n "$NAMESPACE" -- python -c "
        import asyncio
        import json

        async def test_websocket():
            try:
                import websockets
                async with websockets.connect('ws://localhost:8001/ws/') as websocket:
                    await websocket.send(json.dumps({'type': 'ping', 'timestamp': '2025-11-20T00:00:00Z'}))
                    response = await websocket.recv()
                    print(f'Success: {response}')
                    return True
            except Exception as e:
                print(f'Error: {e}')
                return False

        result = asyncio.run(test_websocket())
        exit(0 if result else 1)
    " || {
        log_error "WebSocket performance test failed."
        exit 1
    }

    log_success "Performance tests completed."
}

# Deploy monitoring stack
deploy_monitoring() {
    if [ "$MONITORING_ENABLED" = "true" ]; then
        log_info "Deploying monitoring stack..."

        # Apply monitoring manifests
        kubectl apply -f kubernetes/monitoring/

        # Wait for monitoring pods to be ready
        kubectl wait --for=condition=ready pod -l app=prometheus -n "$NAMESPACE" --timeout=300s
        kubectl wait --for=condition=ready pod -l app=grafana -n "$NAMESPACE" --timeout=300s

        log_success "Monitoring stack deployed successfully."

        # Print monitoring URLs
        log_info "Monitoring URLs:"
        kubectl get ingress -n "$NAMESPACE" -l app=monitoring -o jsonpath='{.items[0].spec.rules[0].host}' | xargs -I {} echo "  Grafana: http://{}"
        kubectl get ingress -n "$NAMESPACE" -l app=monitoring -o jsonpath='{.items[0].spec.rules[1].host}' | xargs -I {} echo "  Prometheus: http://{}"
    fi
}

# Deploy blue-green deployment
deploy_blue_green() {
    if [ "$DEPLOYMENT_STRATEGY" = "blue-green" ]; then
        log_info "Deploying with blue-green strategy..."

        # Deploy blue environment
        log_info "Deploying blue environment..."
        kubectl apply -f kubernetes/overlays/production/deployment-blue-green.yaml

        # Wait for blue pods to be ready
        kubectl wait --for=condition=ready pod -l app=backend,color=blue -n "$NAMESPACE" --timeout=300s
        kubectl wait --for=condition=ready pod -l app=frontend,color=blue -n "$NAMESPACE" --timeout=300s

        # Run health checks on blue environment
        log_info "Running health checks on blue environment..."
        kubectl exec deployment/backend-blue -n "$NAMESPACE" -- curl -f http://localhost:8000/health || {
            log_error "Blue environment health check failed."
            exit 1
        }

        # Deploy green environment
        log_info "Deploying green environment..."
        kubectl apply -f kubernetes/overlays/production/deployment-blue-green.yaml

        # Wait for green pods to be ready
        kubectl wait --for=condition=ready pod -l app=backend,color=green -n "$NAMESPACE" --timeout=300s
        kubectl wait --for=condition=ready pod -l app=frontend,color=green -n "$NAMESPACE" --timeout=300s

        # Run health checks on green environment
        log_info "Running health checks on green environment..."
        kubectl exec deployment/backend-green -n "$NAMESPACE" -- curl -f http://localhost:8000/health || {
            log_error "Green environment health check failed."
            exit 1
        }

        # Switch traffic to green environment
        log_info "Switching traffic to green environment..."
        kubectl patch service backend-service -n "$NAMESPACE" --type='json' -p '[{"op": "replace", "path": "/spec/selector/color", "value": "green"}]'
        kubectl patch service frontend-service -n "$NAMESPACE" --type='json' -p '[{"op": "replace", "path": "/spec/selector/color", "value": "green"}]'

        log_success "Blue-green deployment completed successfully."
    fi
}

# Run rollback
rollback() {
    log_info "Performing rollback..."

    # Get last known good deployment
    if kubectl get deployment backend-rollback -n "$NAMESPACE" &> /dev/null; then
        log_info "Rolling back to previous version..."
        kubectl rollout undo deployment/backend -n "$NAMESPACE"
        kubectl rollout undo deployment/frontend -n "$NAMESPACE"
    else
        log_info "No backup deployment found. Scaling down services..."
        kubectl scale deployment/backend --replicas=0 -n "$NAMESPACE"
        kubectl scale deployment/frontend --replicas=0 -n "$NAMESPACE"
    fi

    log_success "Rollback completed successfully."
}

# Show deployment status
show_status() {
    log_info "Deployment status:"

    # Show pod status
    echo -e "\n${BLUE}Pod Status:${NC}"
    kubectl get pods -n "$NAMESPACE" -l app in (backend, frontend)

    # Show service status
    echo -e "\n${BLUE}Service Status:${NC}"
    kubectl get services -n "$NAMESPACE"

    # Show ingress status
    echo -e "\n${BLUE}Ingress Status:${NC}"
    kubectl get ingress -n "$NAMESPACE"

    # Show resource usage
    echo -e "\n${BLUE}Resource Usage:${NC}"
    kubectl top pods -n "$NAMESPACE"

    # Show events
    echo -e "\n${BLUE}Recent Events:${NC}"
    kubectl get events -n "$NAMESPACE" --sort-by='.metadata.creationTimestamp' | tail -10
}

# Main deployment function
main() {
    log_info "Starting deployment of Multimodal RAG System..."
    log_info "Namespace: $NAMESPACE"
    log_info "Environment: $ENVIRONMENT"
    log_info "Deployment Strategy: $DEPLOYMENT_STRATEGY"
    log_info "Monitoring: $MONITORING_ENABLED"
    log_info "Security Scanning: $SECURITY_SCANNING"

    # Check prerequisites
    check_prerequisites

    # Create namespace
    create_namespace

    # Apply Kubernetes manifests
    apply_kubernetes_manifests

    # Build and push Docker images
    build_and_push_images

    # Run security scans
    run_security_scans

    # Run health checks
    run_health_checks

    # Run performance tests
    run_performance_tests

    # Deploy monitoring stack
    deploy_monitoring

    # Deploy blue-green deployment
    deploy_blue_green

    # Show deployment status
    show_status

    log_success "Deployment completed successfully!"

    # Print next steps
    echo -e "\n${BLUE}Next Steps:${NC}"
    echo "1. Access the application at: http://localhost:3000"
    echo "2. Access the API documentation at: http://localhost:8000/docs"
    echo "3. Access the monitoring dashboard at: http://localhost:3000 (Grafana)"
    echo "4. Access Prometheus at: http://localhost:9090"
    echo "5. Check the deployment status: ./deploy.sh --status"
    echo "6. View logs: kubectl logs -f deployment/backend -n $NAMESPACE"
    echo "7. Scale the deployment: kubectl scale deployment/backend --replicas=5 -n $NAMESPACE"
    echo "8. Rollback: ./deploy.sh --rollback"
}

# Parse command line arguments
case "${1:-}" in
    "--help"|"help")
        echo "Usage: $0 [OPTIONS]"
        echo "Options:"
        echo "  --help              Show this help message"
        echo "  --status            Show deployment status"
        echo "  --rollback          Perform rollback"
        echo "  --namespace NAMESPACE  Specify namespace (default: multimodal-rag-system)"
        echo "  --environment ENV   Specify environment (default: staging)"
        echo "  --strategy STRATEGY Specify deployment strategy (default: rolling-update)"
        echo "  --no-monitoring     Disable monitoring stack"
        echo "  --no-security       Disable security scanning"
        exit 0
        ;;
    "--status")
        show_status
        exit 0
        ;;
    "--rollback")
        rollback
        exit 0
        ;;
    --namespace*)
        NAMESPACE="${1#--namespace=}"
        shift
        ;;
    --environment*)
        ENVIRONMENT="${1#--environment=}"
        shift
        ;;
    --strategy*)
        DEPLOYMENT_STRATEGY="${1#--strategy=}"
        shift
        ;;
    --no-monitoring)
        MONITORING_ENABLED="false"
        shift
        ;;
    --no-security)
        SECURITY_SCANNING="false"
        shift
        ;;
    *)
        # No arguments, proceed with deployment
        ;;
esac

# Run main function
main "$@"
