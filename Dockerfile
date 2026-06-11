FROM node:22-alpine AS node

WORKDIR /app

COPY web ./web

RUN cd web && npm config set registry https://registry.npmmirror.com && npm install && npx vite build

# Build nsjail from source so the image ships a self-contained sandbox backend
# that needs no host Docker socket. Pinned to a release tag for reproducibility.
# Multi-stage keeps the compile toolchain (bison/flex/protobuf-dev/libnl-dev)
# out of the final image; only the nsjail binary and its small runtime libs
# (libprotobuf, libnl-route-3) are carried over.
FROM python:3.12.7-slim AS nsjail-build

ARG NSJAIL_VERSION=3.6

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates git build-essential \
        autoconf bison flex libtool pkg-config \
        protobuf-compiler libprotobuf-dev libnl-route-3-dev \
    && git clone --depth 1 --branch "${NSJAIL_VERSION}" https://github.com/google/nsjail.git /nsjail \
    && make -C /nsjail \
    && install -m 0755 /nsjail/nsjail /usr/local/bin/nsjail \
    && rm -rf /var/lib/apt/lists/*

FROM python:3.12.7-slim

RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources && sed -i 's/security.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources

WORKDIR /app

COPY . .

COPY --from=node /app/web/dist ./web/dist

# nsjail binary built in the dedicated stage above. Self-contained sandbox
# backend; lets the Box runtime isolate code without a host Docker socket.
COPY --from=nsjail-build /usr/local/bin/nsjail /usr/local/bin/nsjail

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc ca-certificates curl gnupg \
    # nsjail runtime libraries (the build toolchain stays in the nsjail-build
    # stage; only these shared libs are needed to execute the binary).
    && apt-get install -y --no-install-recommends libprotobuf32 libnl-route-3-200 \
    # Install the Docker CLI (client only) so the optional langbot_box
    # service can drive the mounted host Docker socket and create sandbox
    # containers. The same image powers langbot / plugin_runtime / box; only
    # box uses the client. Arch-aware via dpkg so multi-arch builds work.
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && python -m pip install --no-cache-dir uv  -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && uv sync -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/* \
    && touch /.dockerenv

EXPOSE 5300
EXPOSE 2280-2285
EXPOSE 5401

VOLUME /app/data
VOLUME /app/data/plugins

CMD [ "uv", "run", "--no-sync", "main.py" ]