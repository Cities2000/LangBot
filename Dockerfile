FROM node:22-alpine AS node

WORKDIR /app

COPY web ./web

RUN cd web && npm config set registry https://registry.npmmirror.com && npm install && npx vite build

FROM python:3.12.7-slim

RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources && sed -i 's/security.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources

WORKDIR /app

COPY . .

COPY --from=node /app/web/dist ./web/dist

RUN apt update \
    && apt install gcc -y \
    && python -m pip install --no-cache-dir uv -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && uv sync --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    && touch /.dockerenv

EXPOSE 5300
EXPOSE 2280-2285
EXPOSE 5401

VOLUME /app/data
VOLUME /app/data/plugins

CMD [ "uv", "run", "--no-sync", "main.py" ]