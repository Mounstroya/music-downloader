# Use Python 3.11 for better compatibility (avoid 3.13 issues with pydantic)
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

# rustypipe-botguard: genera los PO tokens de YouTube que usa el plugin yt-dlp-get-pot-rustypipe
ARG BOTGUARD_VERSION=v0.1.2
RUN curl -fsSL "https://codeberg.org/ThetaDev/rustypipe-botguard/releases/download/${BOTGUARD_VERSION}/rustypipe-botguard-${BOTGUARD_VERSION}-x86_64-unknown-linux-gnu.tar.xz" \
    | tar -xJ -C /tmp \
    && find /tmp -type f -name rustypipe-botguard -exec install -m 755 {} /usr/local/bin/rustypipe-botguard \; \
    && rm -rf /tmp/rustypipe-botguard* \
    && rustypipe-botguard --version

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY backend/requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY backend/ ./backend/

# Create downloads directory
RUN mkdir -p /app/downloads/temp

# Expose port
EXPOSE 8000

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Run the application
WORKDIR /app/backend
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

