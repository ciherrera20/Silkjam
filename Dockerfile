# Use Python 3.14 slim image
FROM python:3.14-slim

# Set working directory
WORKDIR /app

# Install OS dependencies, including Java 25 JRE
RUN apt-get update &&  apt-get install -y \
    curl \
    bash \
    xz-utils \
    procps \
    openjdk-25-jre-headless \
    supervisor \
    debian-keyring \
    debian-archive-keyring \
    apt-transport-https

# Install Caddy
RUN curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg \
    && curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list \
    && chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg \
    && chmod o+r /etc/apt/sources.list.d/caddy-stable.list \
    && apt-get update \
    && apt-get install caddy \
    && rm -rf /var/lib/apt/lists/*

# Create application user
# UID/GID are adjusted at runtime by entrypoint.sh
RUN groupadd --gid 1000 silkjam \
    && useradd --uid 1000 --gid 1000 --create-home silkjam

# Java configuration
ENV JAVA_HOME=/usr/lib/jvm/java-25-openjdk-amd64
ENV PATH="$JAVA_HOME/bin:$PATH"

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install Python dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

# Copy application
COPY ./app/backend ./backend
COPY ./app/frontend/index.html ./frontend/index.html

# Copy configs
COPY ./app/Caddyfile /etc/caddy/Caddyfile
COPY ./app/supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY ./app/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh

EXPOSE 443
EXPOSE 8000
EXPOSE 25565

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/entrypoint.sh"]