# Traditional Deployment Guide (Without Docker)

> **Status: historical (reconciled 2026-10-17).** This guide was written for earlier deployment generations (Docker Compose, single-host or DigitalOcean/DOKS setups). Current deployment: AWS EKS `nous-dev-cluster` (`us-east-1`), namespace `multimodal-rag-system`, Argo CD application `nous-dev-aws`. Qdrant is removed; live retrieval is PostgreSQL full-text. Start with the [AWS deployment overview](aws-overview.md) and [AWS on-call commands](../operations/aws-on-call.md). The text below is kept unedited as a record; do not run its commands without checking them against the current repo.

This guide shows how to deploy your RAG system **without Docker** using traditional methods.

---

## 🎯 **When to Use This Approach**

- ✅ Small to medium deployments
- ✅ Single server setup
- ✅ Development/testing environments
- ✅ When Docker is overkill for your needs
- ✅ Direct access to logs and debugging needed

---

## 📋 **Prerequisites**

### Server Requirements
- Ubuntu 20.04+ or similar Linux distribution
- Python 3.11+
- PostgreSQL 14+
- Redis 7+
- 4GB+ RAM (8GB recommended)
- 20GB+ disk space

---

## 🔧 **Step 1: Install System Dependencies**

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python and build tools
sudo apt install -y python3.11 python3.11-venv python3-pip \
  build-essential libpq-dev ffmpeg tesseract-ocr

# Install PostgreSQL
sudo apt install -y postgresql postgresql-contrib

# Install Redis
sudo apt install -y redis-server

# Install Neo4j (optional, for knowledge graph)
wget -O - https://debian.neo4j.com/neotechnology.gpg.key | sudo apt-key add -
echo 'deb https://debian.neo4j.com stable latest' | sudo tee /etc/apt/sources.list.d/neo4j.list
sudo apt update
sudo apt install -y neo4j
```

---

## 🗄️ **Step 2: Configure Databases**

### PostgreSQL Setup

```bash
# Create database and user
sudo -u postgres psql << EOF
CREATE DATABASE ragdb;
CREATE USER raguser WITH PASSWORD 'your-secure-password';
GRANT ALL PRIVILEGES ON DATABASE ragdb TO raguser;
ALTER DATABASE ragdb OWNER TO raguser;
\q
EOF

# Enable PostgreSQL to start on boot
sudo systemctl enable postgresql
sudo systemctl start postgresql
```

### Redis Setup

```bash
# Configure Redis
sudo sed -i 's/bind 127.0.0.1/bind 0.0.0.0/' /etc/redis/redis.conf
sudo systemctl enable redis-server
sudo systemctl start redis-server
```

### Neo4j Setup (Optional)

```bash
# Configure Neo4j
sudo systemctl enable neo4j
sudo systemctl start neo4j

# Set initial password
sudo neo4j-admin set-initial-password your-neo4j-password
```

---

## 📦 **Step 3: Deploy Application**

### Clone Repository

```bash
# Create application directory
sudo mkdir -p /opt/rag-system
sudo chown $USER:$USER /opt/rag-system
cd /opt/rag-system

# Clone your repository
git clone https://github.com/goodwiins/rag.git .
```

### Set Up Python Virtual Environment

```bash
# Create virtual environment
python3.11 -m venv venv

# Activate virtual environment
source venv/bin/activate

# Upgrade pip
pip install --upgrade pip

# Install dependencies
cd backend
pip install -r requirements.txt
```

### Configure Environment Variables

```bash
# Create .env file
cat > /opt/rag-system/backend/.env << EOF
# Database
DATABASE_URL=postgresql://raguser:your-secure-password@localhost:5432/ragdb

# Redis
REDIS_URL=redis://localhost:6379/0

# Security
SECRET_KEY=$(openssl rand -hex 32)
ALGORITHM=HS256

# Neo4j (optional)
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-neo4j-password

# Qdrant (if running separately)
QDRANT_URL=http://localhost:6333

# Application
ENVIRONMENT=production
DEBUG=False
LOG_LEVEL=INFO
EOF

# Secure the .env file
chmod 600 /opt/rag-system/backend/.env
```

### Run Database Migrations

```bash
cd /opt/rag-system/backend
source ../venv/bin/activate

# Run migrations
alembic upgrade head
```

---

## 🚀 **Step 4: Set Up Systemd Services**

### Backend API Service

```bash
# Create systemd service file
sudo tee /etc/systemd/system/rag-backend.service > /dev/null << EOF
[Unit]
Description=RAG System Backend API
After=network.target postgresql.service redis-server.service

[Service]
Type=simple
User=$USER
WorkingDirectory=/opt/rag-system/backend
Environment="PATH=/opt/rag-system/venv/bin"
EnvironmentFile=/opt/rag-system/backend/.env
ExecStart=/opt/rag-system/venv/bin/uvicorn src.main:app \\
  --host 0.0.0.0 \\
  --port 8000 \\
  --workers 4 \\
  --log-config logging.conf

Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
```

### Celery Worker Service

```bash
# Create Celery worker service
sudo tee /etc/systemd/system/rag-worker.service > /dev/null << EOF
[Unit]
Description=RAG System Celery Worker
After=network.target redis-server.service

[Service]
Type=simple
User=$USER
WorkingDirectory=/opt/rag-system/backend
Environment="PATH=/opt/rag-system/venv/bin"
EnvironmentFile=/opt/rag-system/backend/.env
ExecStart=/opt/rag-system/venv/bin/celery -A src.tasks.celery_app worker \\
  --loglevel=info \\
  --concurrency=4

Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
```

### Enable and Start Services

```bash
# Reload systemd
sudo systemctl daemon-reload

# Enable services to start on boot
sudo systemctl enable rag-backend
sudo systemctl enable rag-worker

# Start services
sudo systemctl start rag-backend
sudo systemctl start rag-worker

# Check status
sudo systemctl status rag-backend
sudo systemctl status rag-worker
```

---

## 🌐 **Step 5: Set Up Nginx Reverse Proxy**

```bash
# Install Nginx
sudo apt install -y nginx

# Create Nginx configuration
sudo tee /etc/nginx/sites-available/rag-system > /dev/null << EOF
server {
    listen 80;
    server_name your-domain.com;  # Change this

    client_max_body_size 100M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # WebSocket support (if needed)
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    location /health {
        proxy_pass http://127.0.0.1:8000/health;
        access_log off;
    }
}
EOF

# Enable the site
sudo ln -s /etc/nginx/sites-available/rag-system /etc/nginx/sites-enabled/

# Test configuration
sudo nginx -t

# Restart Nginx
sudo systemctl restart nginx
```

### Optional: Set Up SSL with Let's Encrypt

```bash
# Install Certbot
sudo apt install -y certbot python3-certbot-nginx

# Get SSL certificate
sudo certbot --nginx -d your-domain.com

# Auto-renewal is set up automatically
```

---

## 📊 **Step 6: Monitoring & Logging**

### View Logs

```bash
# Backend logs
sudo journalctl -u rag-backend -f

# Worker logs
sudo journalctl -u rag-worker -f

# Nginx logs
sudo tail -f /var/log/nginx/access.log
sudo tail -f /var/log/nginx/error.log
```

### Set Up Log Rotation

```bash
# Create logrotate config
sudo tee /etc/logrotate.d/rag-system > /dev/null << EOF
/var/log/rag-system/*.log {
    daily
    rotate 14
    compress
    delaycompress
    notifempty
    create 0640 $USER $USER
    sharedscripts
    postrotate
        systemctl reload rag-backend > /dev/null 2>&1 || true
        systemctl reload rag-worker > /dev/null 2>&1 || true
    endscript
}
EOF
```

### Basic Monitoring

```bash
# Install monitoring tools
pip install psutil

# Create simple health check script
cat > /opt/rag-system/health-check.sh << 'EOF'
#!/bin/bash
curl -f http://localhost:8000/health || systemctl restart rag-backend
EOF

chmod +x /opt/rag-system/health-check.sh

# Add to crontab (runs every 5 minutes)
(crontab -l 2>/dev/null; echo "*/5 * * * * /opt/rag-system/health-check.sh") | crontab -
```

---

## 🔄 **Step 7: Deployment Updates**

### Manual Update Process

```bash
#!/bin/bash
# save as /opt/rag-system/update.sh

set -e

cd /opt/rag-system

echo "Pulling latest changes..."
git pull origin main

echo "Activating virtual environment..."
source venv/bin/activate

echo "Installing dependencies..."
cd backend
pip install -r requirements.txt

echo "Running migrations..."
alembic upgrade head

echo "Restarting services..."
sudo systemctl restart rag-backend
sudo systemctl restart rag-worker

echo "Waiting for services to start..."
sleep 5

echo "Health check..."
curl -f http://localhost:8000/health

echo "Update completed successfully!"
```

### Automated Updates via GitHub Actions

Use the `ci-no-docker.yml` workflow provided earlier.

---

## 🔐 **Security Hardening**

### Firewall Setup

```bash
# Enable UFW
sudo ufw enable

# Allow SSH
sudo ufw allow 22/tcp

# Allow HTTP/HTTPS
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# Deny direct access to backend (Nginx will proxy)
sudo ufw deny 8000/tcp

# Check status
sudo ufw status
```

### Secure PostgreSQL

```bash
# Edit PostgreSQL config
sudo nano /etc/postgresql/14/main/pg_hba.conf

# Change to:
# local   all             all                                     md5
# host    all             all             127.0.0.1/32            md5

# Restart PostgreSQL
sudo systemctl restart postgresql
```

---

## 📈 **Performance Optimization**

### Gunicorn (Alternative to Uvicorn)

```bash
# Install Gunicorn with Uvicorn workers
pip install gunicorn

# Update systemd service
sudo nano /etc/systemd/system/rag-backend.service

# Change ExecStart to:
# ExecStart=/opt/rag-system/venv/bin/gunicorn src.main:app \\
#   --workers 4 \\
#   --worker-class uvicorn.workers.UvicornWorker \\
#   --bind 0.0.0.0:8000 \\
#   --timeout 120
```

### PostgreSQL Tuning

```bash
# Edit PostgreSQL config
sudo nano /etc/postgresql/14/main/postgresql.conf

# Recommended settings for 8GB RAM:
# shared_buffers = 2GB
# effective_cache_size = 6GB
# maintenance_work_mem = 512MB
# checkpoint_completion_target = 0.9
# wal_buffers = 16MB
# default_statistics_target = 100
# random_page_cost = 1.1
# effective_io_concurrency = 200

# Restart
sudo systemctl restart postgresql
```

---

## 🆘 **Troubleshooting**

### Service Won't Start

```bash
# Check service status
sudo systemctl status rag-backend
sudo systemctl status rag-worker

# Check logs
sudo journalctl -u rag-backend -n 100 --no-pager
sudo journalctl -u rag-worker -n 100 --no-pager
```

### Database Connection Issues

```bash
# Test PostgreSQL connection
psql postgresql://raguser:password@localhost:5432/ragdb

# Check if PostgreSQL is running
sudo systemctl status postgresql
```

### Port Already in Use

```bash
# Find what's using port 8000
sudo lsof -i :8000

# Kill the process
sudo kill -9 <PID>
```

---

## 📊 **Comparison: Traditional vs Docker**

| Aspect | Traditional | Docker |
|--------|-------------|--------|
| **Setup Time** | 30-60 min | 5-10 min |
| **Debugging** | Easy | Medium |
| **Resource Usage** | Lower | Higher |
| **Consistency** | Manual | Automatic |
| **Scaling** | Manual | Easy |
| **Best For** | Single server | Multi-server |

---

## 🎓 **Next Steps**

1. ✅ Set up monitoring (Prometheus, Grafana)
2. ✅ Configure automated backups
3. ✅ Set up proper logging aggregation
4. ✅ Implement auto-scaling (if needed)
5. ✅ Add CDN for static assets

---

**Questions?** Check the [CI/CD Best Practices](CI_CD_BEST_PRACTICES.md) for more information.
