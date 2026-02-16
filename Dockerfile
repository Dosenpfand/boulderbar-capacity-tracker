FROM python:3.11-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml .
RUN uv sync --no-dev
COPY app.py .
COPY templates/ templates/
RUN mkdir -p /data
ENV PYTHONUNBUFFERED=1
ENV DB_PATH=/data
EXPOSE 5000
CMD ["uv", "run", "gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "app:app"]
