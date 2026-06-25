FROM python:3.11-slim
WORKDIR /app
RUN pip install httpx asyncio
COPY main.py .
CMD ["python", "main.py"]
