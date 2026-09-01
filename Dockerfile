FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p sessions

# sessions и bot.db переживают рестарт (volume)
VOLUME ["/app/sessions", "/app"]

CMD ["python", "bot.py"]
