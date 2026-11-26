FROM python:3.12-slim AS wheels
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY packages/ ./packages/
RUN pip install --no-cache-dir --upgrade pip \
    && pip wheel --no-cache-dir --no-deps --wheel-dir /wheels \
        ./packages/etl-migration-core \
        ./packages/etl-portfolio-assessment \
        ./packages/etl-target-fabric \
        .

FROM python:3.12-slim
WORKDIR /app
RUN groupadd --system accelerator \
    && useradd --system --gid accelerator --home-dir /app --no-create-home accelerator \
    && mkdir -p /data \
    && chown accelerator:accelerator /app /data
COPY --from=wheels /wheels/*.whl /tmp/wheels/
RUN pip install --no-cache-dir --find-links /tmp/wheels \
        /tmp/wheels/etl_migration_core-*.whl \
        /tmp/wheels/etl_portfolio_assessment-*.whl \
        /tmp/wheels/etl_target_fabric-*.whl \
        /tmp/wheels/fabric_etl_migration_accelerator-*.whl \
    && rm -rf /tmp/wheels
ENV ACCELERATOR_MODE=server
ENV ACCELERATOR_ALLOW_LOCAL_FILESYSTEM=false
ENV ACCELERATOR_STATE_DIR=/data
USER accelerator
EXPOSE 8050
VOLUME /data
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8050/health', timeout=3)"
CMD ["uvicorn", "accelerator.api:app", "--host", "0.0.0.0", "--port", "8050"]
