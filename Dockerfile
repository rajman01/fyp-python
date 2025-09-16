# Use a lightweight Python base image
FROM python:3.13.7

# Set working directory
WORKDIR /app

# Copy requirement files first (better caching)
COPY requirements.txt requirements.txt

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY . .

# Expose the port (Fly will map this automatically)
EXPOSE 8080

# Run with Gunicorn
CMD ["gunicorn", "-b", "0.0.0.0:8080", "app:app"]
