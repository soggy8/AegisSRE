FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sre_swarm ./sre_swarm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
