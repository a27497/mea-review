# MEA Review workbench: offline Mock and frozen snapshots only. No key, no model, no GitHub access.
# Python standard library only; nothing is installed at build time.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LANG=C.UTF-8
WORKDIR /app
COPY LICENSE THIRD_PARTY_NOTICES.md ./
COPY mea ./mea
COPY web ./web
COPY fixtures ./fixtures
USER 65534:65534
EXPOSE 8810
# Listens on all container interfaces; compose publishes the port on the host loopback only, and the
# server still requires Host 127.0.0.1:8810. Tasks live in a tmpfs and vanish with the container.
CMD ["python3", "-B", "-m", "mea", "serve", "--bind", "0.0.0.0", "--port", "8810", "--task-dir", "/tmp/mea-tasks"]
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python3", "-c", "import urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:8810/api/demos', headers={'Host': '127.0.0.1:8810'}), timeout=2)"]
