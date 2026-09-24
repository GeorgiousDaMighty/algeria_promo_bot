FROM python:3.12-slim
WORKDIR /app
RUN useradd --create-home --uid 10001 jet && mkdir /app/data && chown jet:jet /app/data
COPY bot.py /app/bot.py
USER jet
CMD ["python", "-u", "bot.py"]
