ARG BUILD_FROM=ghcr.io/home-assistant/base:latest
FROM ${BUILD_FROM}

# HA のベースイメージは版により Alpine / Debian の両方があるため、両対応にする
RUN if command -v apk >/dev/null 2>&1; then \
        apk add --no-cache python3 py3-pip; \
    else \
        apt-get update \
        && apt-get install -y --no-install-recommends python3 python3-venv python3-pip \
        && rm -rf /var/lib/apt/lists/*; \
    fi \
    && python3 -m venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv
COPY requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt

COPY app /srv/app
COPY entity_catalog.sample.yaml policy.sample.yaml /srv/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
