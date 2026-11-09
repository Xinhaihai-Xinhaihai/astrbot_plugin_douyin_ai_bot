FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    CHROMIUM_PROFILE=/home/chromium/profile

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        curl \
        fonts-noto-cjk \
        novnc \
        tini \
        websockify \
        x11-utils \
        x11vnc \
        xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin chromium \
    && mkdir -p /home/chromium/profile \
    && chown -R chromium:chromium /home/chromium

COPY --chmod=755 entrypoint.sh /usr/local/bin/douyin-browser-entrypoint
USER chromium
WORKDIR /home/chromium

EXPOSE 5900 6080 9222
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD curl --fail --silent http://127.0.0.1:9222/json/version >/dev/null || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/douyin-browser-entrypoint"]
