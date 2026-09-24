FROM python:3.12-slim
WORKDIR /app
RUN useradd --create-home --uid 10001 jet \
    && mkdir /app/data \
    && chown jet:jet /app/data \
    && apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*
COPY bot.py /app/bot.py
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-u", "bot.py"]
