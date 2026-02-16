FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -r -m -s /bin/false appuser && mkdir -p /app/db && chown -R appuser:appuser /app
USER appuser

CMD ["python", "main.py", "-c", "/app/config.yaml", "-v"]
