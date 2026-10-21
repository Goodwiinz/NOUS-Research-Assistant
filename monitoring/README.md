# Monitoring Stack Setup Guide

> **Scope:** this directory is the **local Docker Compose monitoring stack**
> (`docker-compose.monitoring.yml`). It is not deployed to the AWS cluster by
> Argo CD (checked 2026-10-17). Live AWS alerting is defined in
> [`infrastructure/terraform/monitoring.tf`](../infrastructure/terraform/monitoring.tf)
> (CloudWatch alarms and an SNS topic); see
> [`infrastructure/terraform/README.md`](../infrastructure/terraform/README.md).
> Unconsumed legacy cluster manifests live in
> [`deployment/monitoring/`](../deployment/monitoring/README.md). Same-named
> files across these trees are not shared copies.

## Security-First Setup

This monitoring stack requires proper credential management to ensure security. Follow these steps carefully.

### Prerequisites

- Docker and Docker Compose installed
- OpenSSL (for generating secure passwords)

### Initial Setup

1. **Create your environment file**:
   ```bash
   cp .env.example .env
   ```

2. **Generate secure passwords**:
   ```bash
   # Generate a strong password for Grafana admin
   openssl rand -base64 32

   # Generate a strong secret key for Grafana
   openssl rand -base64 32

   # Generate a strong password for Redis
   openssl rand -base64 32
   ```

3. **Edit the .env file** and replace all `CHANGE_ME_IMMEDIATELY` values with the generated passwords:
   ```bash
   nano .env  # or use your preferred editor
   ```

   **CRITICAL**: Update these variables at minimum:
   - `GRAFANA_ADMIN_PASSWORD`
   - `GRAFANA_SECRET_KEY`
   - `REDIS_PASSWORD`

4. **Verify .env is in .gitignore**:
   ```bash
   cat .gitignore | grep "^.env$"
   ```
   The `.env` file should NEVER be committed to version control.

### Starting the Stack

```bash
# Start all services
docker-compose -f docker-compose.monitoring.yml up -d

# Check service health
docker-compose -f docker-compose.monitoring.yml ps

# View logs
docker-compose -f docker-compose.monitoring.yml logs -f
```

### Accessing Services

- **Grafana**: http://localhost:3001
  - Username: Value from `GRAFANA_ADMIN_USER` (default: admin)
  - Password: Value from `GRAFANA_ADMIN_PASSWORD`

- **Prometheus**: http://localhost:9090
- **Jaeger UI**: http://localhost:16686
- **AlertManager**: http://localhost:9093

### Security Checklist

- [ ] `.env` file created with secure passwords
- [ ] All `CHANGE_ME_IMMEDIATELY` values replaced
- [ ] `.env` file is NOT committed to git
- [ ] Grafana admin password is strong (minimum 32 characters recommended)
- [ ] Redis password is configured
- [ ] File permissions on `.env` are restrictive: `chmod 600 .env`

### Optional Alert Configuration

Configure alert notifications by adding these to your `.env`:

**Slack Alerts**:
```bash
ALERTMANAGER_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL
ALERTMANAGER_SLACK_CHANNEL=#alerts
```

**Email Alerts**:
```bash
ALERTMANAGER_SMTP_FROM=alerts@yourdomain.com
ALERTMANAGER_SMTP_SMARTHOST=smtp.gmail.com:587
ALERTMANAGER_SMTP_AUTH_USERNAME=your-email@gmail.com
ALERTMANAGER_SMTP_AUTH_PASSWORD=your-app-password
```

**PagerDuty**:
```bash
ALERTMANAGER_PAGERDUTY_SERVICE_KEY=your-service-key
```

### Troubleshooting

**Services won't start**:
```bash
# Check if .env file exists and has correct values
ls -la .env

# Verify environment variables are loaded
docker-compose -f docker-compose.monitoring.yml config
```

**Authentication failures**:
- Verify credentials in `.env` match what you're using to login
- Check Grafana logs: `docker logs grafana`
- Redis authentication errors: verify `REDIS_PASSWORD` is set

### Data Persistence

By default, data is stored in Docker volumes. To use host paths for easier backup:

1. Uncomment and set these in `.env`:
   ```bash
   PROMETHEUS_DATA_PATH=./data/prometheus
   GRAFANA_DATA_PATH=./data/grafana
   LOKI_DATA_PATH=./data/loki
   REDIS_DATA_PATH=./data/redis
   ALERTMANAGER_DATA_PATH=./data/alertmanager
   ```

2. Create directories with proper permissions:
   ```bash
   mkdir -p data/{prometheus,grafana,loki,redis,alertmanager}
   chmod -R 755 data/
   ```

### Stopping the Stack

```bash
# Stop all services
docker-compose -f docker-compose.monitoring.yml down

# Stop and remove volumes (DELETES ALL DATA)
docker-compose -f docker-compose.monitoring.yml down -v
```

## Security Best Practices

1. **Never commit `.env` files** - They contain sensitive credentials
2. **Rotate passwords regularly** - Update credentials every 90 days
3. **Use strong passwords** - Minimum 32 characters, generated randomly
4. **Restrict network access** - Use firewalls to limit who can access monitoring services
5. **Enable HTTPS** - Configure reverse proxy with SSL/TLS in production
6. **Monitor access logs** - Review who is accessing monitoring dashboards
7. **Backup encrypted** - Encrypt backups of monitoring data

## Support

For issues or questions:
- Check service logs: `docker-compose logs <service-name>`
- Review container health: `docker-compose ps`
- Inspect configurations: `docker-compose config`
