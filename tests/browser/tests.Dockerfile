# Playwright with Chromium, the test packages, and ffmpeg for turning a failed test's video
# into a GIF.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/browser-requirements.txt
RUN pip install --no-cache-dir --break-system-packages -r /tmp/browser-requirements.txt
