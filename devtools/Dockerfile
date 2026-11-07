ARG HA_VERSION=2026.11.0
FROM ghcr.io/home-assistant/home-assistant:${HA_VERSION}
COPY devtools/requirements-quality.txt /tmp/requirements-quality.txt
RUN uv pip install --system --no-cache -r /tmp/requirements-quality.txt
WORKDIR /project
ENV PYTHONDONTWRITEBYTECODE=1
ENV MYPYPATH=/usr/src/homeassistant
