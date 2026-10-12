FROM python:3.12-slim
WORKDIR /app
# Paths inside the container. data/config.toml is shared with the Mac; only locations are overridden here.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp \
    CDS_ENV=nas \
    CDS_CONFIG=/data/config.toml \
    CDS_TOKEN_FILE=/secrets/token.json \
    CDS_OUTPUT_ROOT=/output \
    CDS_STATE_ROOT=/data/state \
    CDS_ARCHIVE_ROOT=/data/archive \
    CDS_ADMIN_DIR=/data \
    CDS_UMASK=0002
COPY --chmod=644 requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chmod=644 server.py sync.py sync_state.py list_folder.py ./
COPY markitdown_ext ./markitdown_ext
COPY cds_admin ./cds_admin
RUN chmod -R a=rX /app && mkdir /data /output /secrets
# Non-root default. docker-compose.yaml runs it as the app's NAS user (CDS_UID / CDS_GID).
USER 10001:10001
EXPOSE 8000
# Admin UI + scheduler. The sync itself runs as a child process (python sync.py).
CMD ["python", "server.py"]
