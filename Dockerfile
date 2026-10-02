FROM python:3.13-slim-bookworm

ARG GIT_SHA=unknown
ENV NILO_BUILD_SHA=$GIT_SHA

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        gosu \
        openssh-client \
        rsync \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 1000 --shell /usr/sbin/nologin nilo

WORKDIR /opt/nilo-datalake

COPY pyproject.toml README.md ./
COPY src ./src
COPY config/settings.docker.yaml /etc/nilo-datalake/settings.yaml
COPY deploy/docker/entrypoint.sh /entrypoint.sh

RUN pip install --no-cache-dir . \
    && chmod 755 /entrypoint.sh \
    && mkdir -p /data \
    && chown nilo:nilo /data

ENV NILO_CONFIG=/data/config/settings.yaml \
    NILO_BOOTSTRAP_CONFIG=/etc/nilo-datalake/settings.yaml
EXPOSE 8088
VOLUME ["/data"]

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=8 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/v1/health')"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["nilo-datalake", "serve"]
