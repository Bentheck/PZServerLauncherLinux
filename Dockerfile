FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PZSL_BIND_HOST=0.0.0.0
ENV PZSL_BIND_PORT=48231
ENV PZSL_DATA_ROOT=/var/lib/pzserverlauncher
ENV PZSL_LOGS_ROOT=/var/log/pzserverlauncher

RUN apt-get update \
    && apt-get install -y --no-install-recommends software-properties-common ca-certificates \
    && dpkg --add-architecture i386 \
    && add-apt-repository multiverse \
    && apt-get update \
    && printf "steam steam/question select I AGREE\nsteam steam/license note\n" | debconf-set-selections \
    && apt-get install -y --no-install-recommends python3 python3-venv python3-pip steamcmd \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --system --home /opt/pzserverlauncher --shell /usr/sbin/nologin pzlauncher \
    && mkdir -p /opt/pzserverlauncher /var/lib/pzserverlauncher /var/log/pzserverlauncher \
    && chown -R pzlauncher:pzlauncher /opt/pzserverlauncher /var/lib/pzserverlauncher /var/log/pzserverlauncher

WORKDIR /opt/pzserverlauncher

COPY pyproject.toml README.md ./
COPY app ./app

RUN python3 -m venv .venv \
    && .venv/bin/python -m pip install --upgrade pip \
    && .venv/bin/python -m pip install .

USER pzlauncher

EXPOSE 48231

CMD ["/opt/pzserverlauncher/.venv/bin/pzserverlauncherlinux"]
