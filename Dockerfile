# Jade backend (API + the agents' tool server) as one container image.
# Runs on Azure App Service for Containers / Azure Container Apps, or any
# container host. All state lives outside the container: PostgreSQL
# (JDE_DATABASE_URL) and Blob Storage (JDE_BLOB_CONTAINER_URL). See
# docs/AZURE_DEPLOYMENT.md.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    JDE_API_REPO_ROOT=/app \
    JDE_API_DATA_DIR=/data \
    JDE_COOKIE_SECURE=true

WORKDIR /app
# Source only: agent definitions, capability catalogue, both packages.
COPY .claude/agents ./.claude/agents
COPY .mcp.json capability_catalog.json ./
COPY mcp_server ./mcp_server
COPY api_service/pyproject.toml ./api_service/pyproject.toml
COPY api_service/jde_api_service ./api_service/jde_api_service

RUN pip install -e "./mcp_server[postgres]" -e "./api_service[postgres,azure]" \
    && useradd --create-home --uid 10001 jade \
    && mkdir -p /data && chown jade:jade /data

USER jade
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"
# --proxy-headers: Azure's front end terminates TLS and forwards the request.
CMD ["uvicorn", "jde_api_service.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
