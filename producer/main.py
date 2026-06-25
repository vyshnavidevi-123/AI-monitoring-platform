"""
Data Producer Service
Simulates 500-1000 servers sending CPU, memory, latency, error rate metrics
"""

import asyncio
import random
import time
import math
import httpx
import logging
from datetime import datetime
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [PRODUCER] %(message)s")
logger = logging.getLogger(__name__)

API_GATEWAY_URL = "http://api-gateway:8000"
NUM_SERVERS = 100          # Set to 500-1000 for production
SEND_INTERVAL = 2          # seconds between metric batches
BATCH_SIZE = 20            # servers per batch request

# Anomaly injection: some servers will misbehave
ANOMALY_SERVERS = set(random.sample(range(NUM_SERVERS), k=max(1, NUM_SERVERS // 20)))


def generate_metric(server_id: int, tick: int) -> dict:
    """Generate realistic metrics with occasional anomalies."""
    is_anomaly = server_id in ANOMALY_SERVERS and tick % 30 < 5

    if is_anomaly:
        cpu = random.uniform(85, 100)
        memory = random.uniform(80, 98)
        latency = random.uniform(800, 3000)
        error_rate = random.uniform(10, 50)
    else:
        # Normal with slight diurnal pattern
        base_cpu = 30 + 15 * math.sin(tick / 60 * math.pi)
        cpu = max(0, min(100, base_cpu + random.gauss(0, 5)))
        memory = max(0, min(100, 50 + random.gauss(0, 8)))
        latency = max(1, random.gauss(120, 30))
        error_rate = max(0, random.gauss(0.5, 0.3))

    return {
        "server_id": f"srv-{server_id:04d}",
        "timestamp": datetime.utcnow().isoformat(),
        "cpu_usage": round(cpu, 2),
        "memory_usage": round(memory, 2),
        "latency_ms": round(latency, 2),
        "error_rate": round(error_rate, 4),
        "region": random.choice(["us-east", "us-west", "eu-west", "ap-south"]),
        "service": random.choice(["web", "api", "db", "cache", "worker"]),
    }


async def send_batch(client: httpx.AsyncClient, metrics: list[dict]) -> bool:
    try:
        resp = await client.post(
            f"{API_GATEWAY_URL}/metrics/batch",
            json={"metrics": metrics},
            timeout=10.0
        )
        return resp.status_code == 200
    except Exception as e:
        logger.warning(f"Failed to send batch: {e}")
        return False


async def produce():
    tick = 0
    async with httpx.AsyncClient() as client:
        # Wait for API gateway to be ready
        for _ in range(30):
            try:
                r = await client.get(f"{API_GATEWAY_URL}/health", timeout=3)
                if r.status_code == 200:
                    logger.info("API Gateway is ready. Starting metric production.")
                    break
            except Exception:
                await asyncio.sleep(2)

        while True:
            start = time.monotonic()
            all_metrics = [generate_metric(i, tick) for i in range(NUM_SERVERS)]

            # Send in batches
            tasks = []
            for i in range(0, len(all_metrics), BATCH_SIZE):
                batch = all_metrics[i:i + BATCH_SIZE]
                tasks.append(send_batch(client, batch))

            results = await asyncio.gather(*tasks)
            success = sum(results)
            logger.info(f"Tick {tick}: Sent {NUM_SERVERS} metrics in {len(tasks)} batches ({success}/{len(tasks)} ok)")

            elapsed = time.monotonic() - start
            await asyncio.sleep(max(0, SEND_INTERVAL - elapsed))
            tick += 1


if __name__ == "__main__":
    asyncio.run(produce())
