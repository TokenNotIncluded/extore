FROM python:3.12-slim
RUN pip install --no-cache-dir uv==0.12.20 && useradd --uid 10001 --create-home extore
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY extore ./extore
COPY scripts ./scripts
RUN uv sync --frozen --no-dev && chown -R extore:extore /app
ENV PATH="/app/.venv/bin:$PATH" EXTORE_DATA=/data EXTORE_SCRIPTS=/app/scripts PYTHONUNBUFFERED=1
USER extore
EXPOSE 8000
CMD ["uvicorn", "extore.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=127.0.0.1"]
