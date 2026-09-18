FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Store the SQLite DB on a mounted volume so listings survive redeploys.
ENV DB_PATH=/data/swaps.db
RUN mkdir -p /data

CMD ["python", "bot.py"]
