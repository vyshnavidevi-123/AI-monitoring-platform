"""
API Gateway Service
- Receives metrics via REST
- Writes to MySQL (long-term) and Redis (hot cache)
- Calls ML service for anomaly detection
- Exposes dashboard and Prometheus endpoints
"""

import os, json, time, logging
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import Optional

import redis.asyncio as aioredis
import aiomysql
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s [API-GW] %(message)s")
logger = logging.getLogger(__name__)

#Config 
REDIS_URL   = os.getenv("REDIS_URL", "redis://redis:6379")
MYSQL_HOST  = os.getenv("MYSQL_HOST", "mysql")
MYSQL_PORT  = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER  = os.getenv("MYSQL_USER", "monitor")
MYSQL_PASS  = os.getenv("MYSQL_PASS", "monitor123")
MYSQL_DB    = os.getenv("MYSQL_DB",   "monitoring")
ML_SERVICE  = os.getenv("ML_SERVICE_URL", "http://ml-service:8001")

#Prometheus metrics 
METRICS_RECEIVED   = Counter("metrics_received_total", "Total metric data points received")
ANOMALIES_DETECTED = Counter("anomalies_detected_total", "Total anomalies", ["severity"])
API_LATENCY        = Histogram("api_request_duration_seconds", "API request duration", ["endpoint"])
CACHE_HITS         = Counter("cache_hits_total", "Redis cache hits")
CACHE_MISSES       = Counter("cache_misses_total", "Redis cache misses")
ACTIVE_SERVERS     = Gauge("active_servers", "Number of servers reporting metrics")

#Global connections 
redis_client: Optional[aioredis.Redis] = None
db_pool: Optional[aiomysql.Pool] = None
http_client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, db_pool, http_client
    # Redis
    redis_client = await aioredis.from_url(REDIS_URL, decode_responses=True)
    # MySQL — retry until ready
    for attempt in range(30):
        try:
            db_pool = await aiomysql.create_pool(
                host=MYSQL_HOST, port=MYSQL_PORT,
                user=MYSQL_USER, password=MYSQL_PASS,
                db=MYSQL_DB, minsize=5, maxsize=20, autocommit=True
            )
            await init_db()
            logger.info("MySQL connected")
            break
        except Exception as e:
            logger.warning(f"MySQL not ready ({attempt}/30): {e}")
            time.sleep(3)
    http_client = httpx.AsyncClient()
    logger.info("API Gateway ready")
    yield
    if db_pool:   db_pool.close(); await db_pool.wait_closed()
    if redis_client: await redis_client.aclose()
    if http_client:  await http_client.aclose()


app = FastAPI(title="AI Monitoring Platform", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


#DB Init
async def init_db():
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS servers (
                    id VARCHAR(20) PRIMARY KEY,
                    region VARCHAR(20),
                    service VARCHAR(20),
                    first_seen DATETIME DEFAULT NOW(),
                    last_seen DATETIME DEFAULT NOW()
                )
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS metrics (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    server_id VARCHAR(20) NOT NULL,
                    timestamp DATETIME NOT NULL,
                    cpu_usage FLOAT,
                    memory_usage FLOAT,
                    latency_ms FLOAT,
                    error_rate FLOAT,
                    anomaly_score FLOAT DEFAULT 0,
                    severity ENUM('normal','warning','critical') DEFAULT 'normal',
                    INDEX idx_server_time (server_id, timestamp),
                    INDEX idx_severity (severity)
                )
            """)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    server_id VARCHAR(20),
                    timestamp DATETIME DEFAULT NOW(),
                    severity ENUM('warning','critical'),
                    message TEXT,
                    acknowledged BOOLEAN DEFAULT FALSE,
                    INDEX idx_severity_time (severity, timestamp)
                )
            """)


#Pydantic schemas
class Metric(BaseModel):
    server_id: str
    timestamp: str
    cpu_usage: float = Field(ge=0, le=100)
    memory_usage: float = Field(ge=0, le=100)
    latency_ms: float = Field(ge=0)
    error_rate: float = Field(ge=0)
    region: str = "unknown"
    service: str = "unknown"

class MetricBatch(BaseModel):
    metrics: list[Metric]

class AlertAck(BaseModel):
    alert_id: int


#Helper: persist one metric
async def persist_metric(m: Metric, anomaly_score: float, severity: str):
    # MySQL
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO servers (id, region, service, last_seen)
                VALUES (%s,%s,%s,NOW())
                ON DUPLICATE KEY UPDATE last_seen=NOW(), region=%s, service=%s
            """, (m.server_id, m.region, m.service, m.region, m.service))

            await cur.execute("""
                INSERT INTO metrics
                    (server_id, timestamp, cpu_usage, memory_usage, latency_ms, error_rate, anomaly_score, severity)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (m.server_id, m.timestamp, m.cpu_usage, m.memory_usage,
                  m.latency_ms, m.error_rate, anomaly_score, severity))

            if severity in ("warning", "critical"):
                msg = (f"CPU:{m.cpu_usage:.1f}% MEM:{m.memory_usage:.1f}% "
                       f"LAT:{m.latency_ms:.0f}ms ERR:{m.error_rate:.2f}%")
                await cur.execute("""
                    INSERT INTO alerts (server_id, severity, message)
                    VALUES (%s,%s,%s)
                """, (m.server_id, severity, msg))
                ANOMALIES_DETECTED.labels(severity=severity).inc()

    # Redis: latest metric (TTL 10 min) and sorted set for dashboard
    pipe = redis_client.pipeline()
    pipe.setex(f"latest:{m.server_id}", 600, json.dumps({
        "server_id": m.server_id, "cpu": m.cpu_usage, "memory": m.memory_usage,
        "latency": m.latency_ms, "error_rate": m.error_rate,
        "severity": severity, "score": anomaly_score, "ts": m.timestamp,
        "region": m.region, "service": m.service
    }))
    pipe.zadd("active_servers", {m.server_id: time.time()})
    pipe.zremrangebyscore("active_servers", 0, time.time() - 600)
    await pipe.execute()


#Routes
@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


@app.post("/metrics/batch")
async def ingest_batch(batch: MetricBatch):
    t0 = time.time()
    METRICS_RECEIVED.inc(len(batch.metrics))

    # Call ML service for anomaly scores
    try:
        resp = await http_client.post(
            f"{ML_SERVICE}/detect",
            json={"metrics": [m.model_dump() for m in batch.metrics]},
            timeout=5.0
        )
        scores = resp.json()["results"]
    except Exception:
        # Fallback: rule-based scoring
        scores = [
            {
                "anomaly_score": _rule_score(m),
                "severity": _rule_severity(m)
            }
            for m in batch.metrics
        ]

    import asyncio
    tasks = [
        persist_metric(m, scores[i]["anomaly_score"], scores[i]["severity"])
        for i, m in enumerate(batch.metrics)
    ]
    await asyncio.gather(*tasks)

    active = await redis_client.zcard("active_servers")
    ACTIVE_SERVERS.set(active)
    API_LATENCY.labels(endpoint="/metrics/batch").observe(time.time() - t0)
    return {"accepted": len(batch.metrics), "latency_ms": round((time.time()-t0)*1000, 1)}


def _rule_score(m: Metric) -> float:
    s = 0.0
    if m.cpu_usage > 80:    s += 0.4
    if m.memory_usage > 80: s += 0.3
    if m.latency_ms > 500:  s += 0.2
    if m.error_rate > 5:    s += 0.1
    return min(1.0, s)

def _rule_severity(m: Metric) -> str:
    s = _rule_score(m)
    if s >= 0.7: return "critical"
    if s >= 0.4: return "warning"
    return "normal"


@app.get("/dashboard/summary")
async def dashboard_summary():
    cache_key = "dashboard:summary"
    cached = await redis_client.get(cache_key)
    if cached:
        CACHE_HITS.inc()
        return json.loads(cached)
    CACHE_MISSES.inc()

    async with db_pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT COUNT(DISTINCT id) as total FROM servers")
            row = await cur.fetchone()
            total_servers = row["total"]

            await cur.execute("""
                SELECT severity, COUNT(*) as cnt
                FROM metrics WHERE timestamp > NOW() - INTERVAL 5 MINUTE
                GROUP BY severity
            """)
            severity_counts = {r["severity"]: r["cnt"] for r in await cur.fetchall()}

            await cur.execute("""
                SELECT AVG(cpu_usage) cpu, AVG(memory_usage) mem,
                       AVG(latency_ms) lat, AVG(error_rate) err
                FROM metrics WHERE timestamp > NOW() - INTERVAL 5 MINUTE
            """)
            avgs = await cur.fetchone()

    result = {
        "total_servers": total_servers,
        "active_servers": await redis_client.zcard("active_servers"),
        "severity": severity_counts,
        "averages": {k: round(v or 0, 2) for k, v in avgs.items()},
        "cached_at": datetime.utcnow().isoformat()
    }
    await redis_client.setex(cache_key, 15, json.dumps(result))
    return result


@app.get("/dashboard/servers")
async def list_servers(page: int = Query(1, ge=1), limit: int = Query(50, le=200)):
    # Try Redis hot cache first
    active_ids = await redis_client.zrevrangebyscore("active_servers", "+inf", "-inf", start=0, num=1000)
    result = []
    for sid in active_ids:
        raw = await redis_client.get(f"latest:{sid}")
        if raw:
            result.append(json.loads(raw))

    if result:
        CACHE_HITS.inc()
    else:
        CACHE_MISSES.inc()
        # Fallback MySQL
        async with db_pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                offset = (page - 1) * limit
                await cur.execute("""
                    SELECT m.server_id, m.cpu_usage, m.memory_usage,
                           m.latency_ms, m.error_rate, m.severity, m.timestamp
                    FROM metrics m
                    INNER JOIN (
                        SELECT server_id, MAX(timestamp) ts
                        FROM metrics GROUP BY server_id
                    ) latest ON m.server_id=latest.server_id AND m.timestamp=latest.ts
                    ORDER BY m.severity DESC LIMIT %s OFFSET %s
                """, (limit, offset))
                result = await cur.fetchall()

    start = (page - 1) * limit
    return {"servers": result[start:start + limit], "total": len(result)}


@app.get("/dashboard/server/{server_id}/history")
async def server_history(server_id: str, hours: int = Query(1, ge=1, le=24)):
    async with db_pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("""
                SELECT timestamp, cpu_usage, memory_usage, latency_ms,
                       error_rate, anomaly_score, severity
                FROM metrics
                WHERE server_id=%s AND timestamp > NOW() - INTERVAL %s HOUR
                ORDER BY timestamp ASC
            """, (server_id, hours))
            rows = await cur.fetchall()
    for r in rows:
        if isinstance(r.get("timestamp"), datetime):
            r["timestamp"] = r["timestamp"].isoformat()
    return {"server_id": server_id, "history": rows}


@app.get("/alerts")
async def get_alerts(severity: Optional[str] = None, limit: int = Query(50, le=500)):
    async with db_pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            if severity:
                await cur.execute("""
                    SELECT * FROM alerts WHERE severity=%s
                    ORDER BY timestamp DESC LIMIT %s
                """, (severity, limit))
            else:
                await cur.execute("""
                    SELECT * FROM alerts ORDER BY timestamp DESC LIMIT %s
                """, (limit,))
            rows = await cur.fetchall()
    for r in rows:
        if isinstance(r.get("timestamp"), datetime):
            r["timestamp"] = r["timestamp"].isoformat()
    return {"alerts": rows, "total": len(rows)}


@app.post("/alerts/acknowledge")
async def acknowledge_alert(body: AlertAck):
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE alerts SET acknowledged=TRUE WHERE id=%s", (body.alert_id,))
    return {"acknowledged": body.alert_id}


@app.get("/metrics/prometheus")
async def prometheus_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
