FROM python:3.13-slim

# DejaVu: кириллица на этикетках
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY bot/requirements.txt bot/requirements.txt
RUN pip install --no-cache-dir -r bot/requirements.txt

COPY inventree_client.py ./
COPY bot ./bot

VOLUME /app/bot_data
CMD ["python", "-m", "bot"]
