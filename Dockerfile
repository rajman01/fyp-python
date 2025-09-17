# Use a lightweight Python base image
FROM python:3.13-slim

RUN apt-get update && apt-get install -y \
    wget \
    libfuse2 \
    xvfb \
    libc6 \
    libfontconfig1 \
    && rm -rf /var/lib/apt/lists/*

# Download ODA File Converter AppImage (replace with the latest version)
RUN wget -O /tmp/ODAFileConverter.AppImage \
    https://www.opendesign.com/guestfiles/get?filename=ODAFileConverter_QT6_lnxX64_8.3dll_26.8.AppImage

# Extract AppImage (avoids FUSE)
RUN chmod +x /tmp/ODAFileConverter.AppImage \
    && /tmp/ODAFileConverter.AppImage --appimage-extract \
    && mv squashfs-root /opt/ODAFileConverter \
    && ln -s /opt/ODAFileConverter/AppRun /usr/local/bin/ODAFileConverter \
    && rm /tmp/ODAFileConverter.AppImage


ENV ODAFILECONVERTER=/usr/local/bin/ODAFileConverter
ENV XDG_RUNTIME_DIR=/tmp/runtime-root
RUN mkdir -p /tmp/runtime-root && chmod 700 /tmp/runtime-root

# Set the working directory
WORKDIR /app

# Copy requirement files first (better caching)
COPY requirements.txt requirements.txt

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY . .

# Expose the port (Fly will map this automatically)
EXPOSE 8080

# Run with Gunicorn
CMD ["gunicorn", "-b", "0.0.0.0:8080", "app:app"]
