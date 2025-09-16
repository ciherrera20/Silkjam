# Use Python 3.12 slim image
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install OS dependencies, including Java 21 JRE
RUN apt-get update && apt-get install -y \
    curl \
    bash \
    xz-utils \
    procps \
    openjdk-21-jre-headless \
    nginx \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

# Set JAVA_HOME (useful for Minecraft scripts)
ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
ENV PATH="$JAVA_HOME/bin:$PATH"

# Install Python dependencies
COPY requirements.txt /
RUN pip install --no-cache-dir -r /requirements.txt

# Copy backend
COPY ./app/backend ./backend

# Copy frontend (index.html)
COPY ./app/frontend ./frontend

# Copy nginx config
COPY ./app/nginx.conf /etc/nginx/nginx.conf

# Copy supervisord config
COPY ./app/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Expose nginx port
EXPOSE 80

# Expose FastAPI port
EXPOSE 8000

# Expose minecraft port
EXPOSE 25565

# Run with Uvicorn
ENV PYTHONUNBUFFERED=1
CMD ["/usr/bin/supervisord"]