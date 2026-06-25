# AI-Powered Scalable Monitoring Platform

A mini Datadog/Grafana clone — real metrics ingestion, ML anomaly detection, Redis caching, MySQL storage, and live dashboards.

---

## Project Structure

```
monitoring-platform/
├── producer/           # Simulates 100-1000 servers sending metrics
│   ├── main.py
│   └── Dockerfile
├── api-gateway/        # FastAPI: receives, stores, queries metrics
│   ├── main.py
│   ├── requirements.txt
│   └── Dockerfile
├── ml-service/         # Isolation Forest anomaly detection
│   ├── main.py
│   ├── requirements.txt
│   └── Dockerfile
├── dashboard/          # Static HTML dashboard (served by Nginx)
│   └── index.html
├── nginx/
│   └── nginx.conf      # Reverse proxy + rate limiting
├── prometheus/
│   └── prometheus.yml  # Scrape config
├── grafana/
│   ├── datasources/    # Auto-configures Prometheus
│   └── dashboards/     # Pre-built dashboard JSON
├── docker/
│   └── init.sql        # MySQL DB + user setup
└── docker-compose.yml  # Entire stack in one file
```

---

## Features
- Real-time metric ingestion from 100–1000 servers
- ML-based anomaly detection using Isolation Forest
- Redis caching for low-latency dashboards
- Prometheus + Grafana monitoring
- Dockerized microservices architecture

---


## Tech Stack
- Backend: Python, FastAPI
- Database: MySQL
- Cache: Redis
- ML: Scikit-learn
- Monitoring: Prometheus, Grafana
- Deployment: Docker, AWS EC2, Nginx

---


## Quick Start 

### Prerequisites
- Docker + Docker Compose installed
- 4 GB RAM free

### Step 1 — Clone / copy the project
```bash
# If you cloned from GitHub:
cd monitoring-platform

# Or just navigate to where you saved the files:
cd /path/to/monitoring-platform
```

### Step 2 — Build and start everything
```bash
docker compose up --build
```

Wait ~60-90 seconds for all services to become healthy.

### Step 3 — Open the apps

| URL | What |
|-----|------|
| http://localhost | Live dashboard |
| http://localhost:8000/docs | API Swagger UI |
| http://localhost:3000 | Grafana (admin / admin123) |
| http://localhost:9090 | Prometheus |

---

## AWS EC2 Deployment (Production)

### Step 1 — Launch an EC2 instance
- **Instance type:** t3.medium (2 vCPU, 4 GB) or larger
- **AMI:** Ubuntu 22.04 LTS
- **Security group inbound rules:**
  - Port 22 (SSH)
  - Port 80 (HTTP)
  - Port 3000 (Grafana — or keep behind nginx)
  - Port 9090 (Prometheus — optional, keep private)

### Step 2 — Install Docker on EC2
```bash
ssh -i your-key.pem ubuntu@<EC2_PUBLIC_IP>

# Install Docker
sudo apt update
sudo apt install -y docker.io docker-compose-plugin git
sudo usermod -aG docker ubuntu
# Log out and back in for group to take effect
exit
ssh -i your-key.pem ubuntu@<EC2_PUBLIC_IP>
```

### Step 3 — Upload project to EC2
```bash
# From your local machine:
scp -i your-key.pem -r monitoring-platform ubuntu@<EC2_PUBLIC_IP>:~/

# Or use git:
git clone https://github.com/yourusername/monitoring-platform.git
```

### Step 4 — Run on EC2
```bash
cd monitoring-platform
docker compose up -d --build
```

### Step 5 — Access via public IP
- Dashboard: `http://<EC2_PUBLIC_IP>`
- Grafana: `http://<EC2_PUBLIC_IP>:3000`

---

## Configuration

### Scale the producer (more simulated servers)
Edit `producer/main.py`:
```python
NUM_SERVERS = 500   # change to 500 or 1000
SEND_INTERVAL = 1   # send every 1 second instead of 2
```

### Tune ML sensitivity
Edit `ml-service/main.py`:
```python
contamination=0.05   # % of data expected to be anomalous (default 5%)
MIN_TRAIN = 500      # samples before ML kicks in (default 500)
```

### Change alert thresholds (rule-based fallback)
Edit `api-gateway/main.py` → `_rule_score()`:
```python
if m.cpu_usage > 80:    s += 0.4   # adjust thresholds here
```

---

## Architecture

```
[Producers x100-1000]
       │  POST /metrics/batch (20 at a time)
       ▼
  [Nginx :80]  ──── static HTML dashboard
       │
  [API Gateway :8000]  ←── Prometheus scrapes metrics
       │            │
       │            └── [Redis :6379]  ← hot cache (TTL 10min)
       │
       ├── POST /detect → [ML Service :8001]
       │                    └── Isolation Forest model
       │
       └── INSERT → [MySQL :3306]
                      ├── servers
                      ├── metrics
                      └── alerts

  [Prometheus :9090] ← scrapes API Gateway + ML Service
  [Grafana :3000]    ← reads Prometheus
```

---

## ML Pipeline

1. **Warm-up phase** (first 500 samples): rule-based scoring
2. **Training**: Isolation Forest trains on 500+ samples in background thread
3. **Inference**: every batch is scored; score 0→1 (higher = more anomalous)
4. **Severity mapping**:
   - `>= 0.65` → Critical alert
   - `>= 0.40` → Warning alert
   - `< 0.40`  → Normal

Model auto-retrains every 5,000 new samples. Saved to `/models/` volume.

---

## Database Schema

```sql
servers  (id, region, service, first_seen, last_seen)
metrics  (id, server_id, timestamp, cpu_usage, memory_usage,
          latency_ms, error_rate, anomaly_score, severity)
alerts   (id, server_id, timestamp, severity, message, acknowledged)
```

---

## Redis Keys

| Key pattern | TTL | Contains |
|-------------|-----|----------|
| `latest:{server_id}` | 10 min | Latest metric JSON |
| `active_servers` | rolling | Sorted set by last-seen timestamp |
| `dashboard:summary` | 15 sec | Cached summary for dashboard |

---

## Prometheus Metrics Exposed

| Metric | Type | Description |
|--------|------|-------------|
| `metrics_received_total` | Counter | Total data points ingested |
| `anomalies_detected_total` | Counter | By severity label |
| `api_request_duration_seconds` | Histogram | Per-endpoint latency |
| `cache_hits_total` | Counter | Redis cache hits |
| `cache_misses_total` | Counter | Redis cache misses |
| `active_servers` | Gauge | Servers active in last 10 min |
