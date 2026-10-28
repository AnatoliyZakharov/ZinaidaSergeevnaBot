FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py db.py auth.py ./

# Директория для SQLite (монтируется как Fly volume)
RUN mkdir -p /data

CMD ["python", "main.py"]
