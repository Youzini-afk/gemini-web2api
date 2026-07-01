FROM golang:1.24.1 AS tls-helper-builder

WORKDIR /src/tls_helper
COPY tls_helper/go.mod tls_helper/go.sum ./
RUN go mod download
COPY tls_helper ./
RUN CGO_ENABLED=0 go build -o /out/gemini-tls-helper .

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    GEMINI_WEB2API_CONFIG=/app/config/config.json

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=tls-helper-builder /out/gemini-tls-helper /usr/local/bin/gemini-tls-helper

# Download mihomo binary (Clash core for proxy node support)
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates wget && \
    ARCH=$(dpkg --print-architecture) && \
    case "$ARCH" in \
        amd64) MIHOMO_ARCH="amd64" ;; \
        arm64) MIHOMO_ARCH="arm64" ;; \
        *) echo "unsupported arch: $ARCH"; MIHOMO_ARCH="" ;; \
    esac && \
    if [ -n "$MIHOMO_ARCH" ]; then \
        echo "Downloading mihomo for $MIHOMO_ARCH..." && \
        wget -qO /tmp/mihomo.gz "https://github.com/MetaCubeX/mihomo/releases/download/v1.19.0/mihomo-linux-${MIHOMO_ARCH}-v1.19.0.gz" || \
        wget -qO /tmp/mihomo.gz "https://github.com/MetaCubeX/mihomo/releases/download/v1.18.10/mihomo-linux-${MIHOMO_ARCH}-v1.18.10.gz" || \
        echo "mihomo download failed, proxy node feature will be unavailable" ; \
        if [ -f /tmp/mihomo.gz ] && [ -s /tmp/mihomo.gz ]; then \
            gunzip -f /tmp/mihomo.gz && \
            mv /tmp/mihomo /usr/local/bin/mihomo && \
            chmod +x /usr/local/bin/mihomo ; \
        fi ; \
    fi && \
    apt-get purge -y wget && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Create persistent config directory
RUN mkdir -p /app/config

COPY gemini_web2api.py ./
COPY gemini_web2api ./gemini_web2api
COPY config.example.json ./config.example.json
COPY README.md README_CN.md LICENSE ./
COPY logo.png ./

EXPOSE 8080

CMD ["sh", "-c", "python gemini_web2api.py --port ${PORT:-8080}"]
