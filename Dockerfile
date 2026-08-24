# Use a lightweight Python base image
FROM python:3.13-slim

# The font packages are what a plan can be drawn in. The app offers a choice
# of family and this image used to carry only the Liberation faces, so every
# choice but one was substituted and every sheet came out looking the same.
#
# liberation2 and croscore are the metric-compatible sets: Liberation
# Sans/Serif/Mono and Arimo/Tinos/Cousine have the same widths as Arial, Times
# New Roman and Courier New. That matters here because a sheet is measured
# from the font's own widths before anything is drawn, so a substitute of
# different proportions moves text that was already fitted to its space.
# dejavu, freefont and urw-base35 add genuinely different designs -- the
# thirty-five PostScript standard faces among them -- so that the families
# the app offers resolve to that many different faces here rather than all
# collapsing onto Liberation Sans.
RUN apt-get update && apt-get install -y \
    wget \
    libfuse2 \
    xvfb \
    libc6 \
    libfontconfig1 \
    fonts-liberation \
    fonts-liberation2 \
    fonts-croscore \
    fonts-dejavu \
    fonts-freefont-ttf \
    fonts-urw-base35 \
    && rm -rf /var/lib/apt/lists/*

# Download ODA File Converter AppImage (replace with the latest version)
# RUN wget -O /tmp/ODAFileConverter.AppImage \
#     https://www.opendesign.com/guestfiles/get?filename=ODAFileConverter_QT6_lnxX64_8.3dll_26.8.AppImage

RUN wget -O /tmp/ODAFileConverter.AppImage \
    https://s3.us-east-2.amazonaws.com/tendar.co/ODAFileConverter_QT6_lnxX64_8.3dll_26.10.AppImage


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

# Run with Gunicorn (worker recycling + long timeouts configured in gunicorn.conf.py)
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
