# Use Python 3.12 slim image
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install OS dependencies, including Java 21 JRE
RUN apt-get update &&  apt-get install -y \
    curl \
    bash \
    xz-utils \
    procps \
    openjdk-21-jre-headless \
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
    && apt-get install caddy

# Remove apt lists
RUN rm -rf /var/lib/apt/lists/*

# Set JAVA_HOME (useful for Minecraft scripts)
ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
ENV PATH="$JAVA_HOME/bin:$PATH"

# Install Python dependencies
COPY requirements.txt /
RUN pip install --no-cache-dir -r /requirements.txt

# Copy backend
COPY ./app/backend ./backend

# Copy frontend (index.html)
COPY ./app/frontend/index.html ./frontend/index.html

# Copy Caddy config
COPY ./app/Caddyfile /etc/caddy/Caddyfile

# Copy supervisord config
COPY ./app/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Expose Caddy port
EXPOSE 443

# Expose FastAPI port
EXPOSE 8000

# Expose minecraft port
EXPOSE 25565

# Run with Uvicorn
ENV PYTHONUNBUFFERED=1
CMD ["/usr/bin/supervisord"]