FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy the miner script
COPY miner.py .

# Install any system dependencies if needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Download and setup XMRig (required for mining)
RUN mkdir -p /app/xmrig && \
    cd /app/xmrig && \
    wget https://github.com/xmrig/xmrig/releases/download/v6.21.0/xmrig-6.21.0-linux-x64.tar.gz && \
    tar xzf xmrig-6.21.0-linux-x64.tar.gz && \
    mv xmrig-6.21.0/xmrig . && \
    chmod +x xmrig && \
    rm -rf xmrig-6.21.0 xmrig-6.21.0-linux-x64.tar.gz

# Set the XMRig path environment variable
ENV XMRIG_PATH=/app/xmrig/xmrig

# Create volume for logs and state files
VOLUME ["/app/logs"]

# Run the miner
CMD ["python", "miner.py"]
