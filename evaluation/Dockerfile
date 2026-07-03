FROM ghcr.io/openclaw/openclaw:2026.3.12

USER root
RUN apt-get update && \
    apt-get install -y --no-install-recommends vim python3-pip && \
    rm -rf /var/lib/apt/lists/*
RUN pip3 install pandas PyYAML beautifulsoup4 requests \
    --break-system-packages

COPY benchmark-mock/ /home/node/.openclaw/extensions/benchmark-mock/

RUN chown -R node:node /home/node/.openclaw

USER node
