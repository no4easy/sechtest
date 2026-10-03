FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server.py index.html admin.html admin-login.html seed_*.json ./
ENV PYTHONUNBUFFERED=1
CMD ["python", "server.py"]
