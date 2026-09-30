FROM python:3.13-slim
WORKDIR /app
RUN useradd --create-home --uid 10001 botuser && mkdir /app/data /app/reports && chown -R botuser:botuser /app
COPY --chown=botuser:botuser bot /app/bot
COPY --chown=botuser:botuser config.toml /app/config.toml
USER botuser
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-m", "bot", "run", "--mode", "paper"]
