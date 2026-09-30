FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY agentmail_imap ./agentmail_imap
COPY scripts ./scripts
COPY certs/global-bundle.pem ./certs/global-bundle.pem
RUN echo 'fe45bbebf92ad3e27a583bbb2ddd1553c521ed4d49af5514dc0a40372ea5395c  certs/global-bundle.pem' | sha256sum -c - \
    && pip install --no-deps . \
    && useradd --uid 10001 --create-home imap \
    && mkdir -p /app/var/tmp && chown -R 10001:10001 /app/var
USER 10001:10001
ENV IMAP_STORAGE_BACKEND=postgres IMAP_HOST=0.0.0.0 IMAP_PORT=1143 IMAP_TLS_MODE=terminated IMAP_TEMP_DIR=/app/var/tmp IMAP_DB_SSLMODE=verify-full IMAP_DB_SSLROOTCERT=/app/certs/global-bundle.pem
EXPOSE 1143 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=6)"
CMD ["python", "-m", "agentmail_imap", "serve"]
