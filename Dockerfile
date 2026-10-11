FROM python:3.13-slim AS builder
WORKDIR /build
COPY pyproject.toml requirements.txt ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels '.[secrets]'

FROM python:3.13-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UW_PROJECT_ROOT=/app
RUN groupadd --gid 10001 underwriting \
    && useradd --uid 10001 --gid 10001 --create-home underwriting \
    && mkdir -p /app/runtime /app/cases /app/reference_docs \
    && chown -R underwriting:underwriting /app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels 'underwriting-platform[secrets]' \
    && rm -r /wheels
WORKDIR /app
USER underwriting
EXPOSE 8000
CMD ["uw-api"]
