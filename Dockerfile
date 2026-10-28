FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    apt-get update -qq && apt-get install -y -qq sqlite3 && rm -rf /var/lib/apt/lists/*

COPY main.py auth.py db.py ./

# Директория для SQLite (монтируется как Fly volume)
RUN mkdir -p /data

CMD ["python", "main.py"]
