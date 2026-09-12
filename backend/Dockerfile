# Reckoner backend — FastAPI + uv (Python 3.13)
FROM python:3.13-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock /app/backend/
WORKDIR /app/backend
RUN uv sync --frozen --no-dev || uv sync --no-dev || uv sync

# App code (DB lives at /app/data/tracker.db — mount a volume there)
COPY backend/ /app/backend/

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
