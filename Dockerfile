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
    CDS_UMASK=0002
COPY --chmod=644 requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chmod=644 sync.py sync_state.py list_folder.py ./
COPY markitdown_ext ./markitdown_ext
RUN chmod -R a=rX /app && mkdir /data /output /secrets
# Dedicated UID/GID for this app (owner of /volume3/.secrets/cloud-drive-sync on the NAS)
USER 28103:28103
CMD ["python", "sync.py"]
