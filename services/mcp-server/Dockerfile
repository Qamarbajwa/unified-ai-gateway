# Build stage
# Pinned to specific sha256 digest for supply chain security
FROM python:3.11-slim@sha256:7bbcfba5a278ec6e1596700c2abac1d01dd37ff65529f7f4fc8ed93b0cbfa6c9 AS builder

WORKDIR /app

# Install dependencies into a virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Final stage - using distroless to reduce attack surface
FROM gcr.io/distroless/python3-debian12@sha256:f5eb4eb609e2cf310c1f6004b77f9852230da3bf27494f3edbd9d854eb13dfbe

WORKDIR /app

# Copy the virtual environment from the builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY . /app

# Run as non-root user
USER nonroot

EXPOSE 8000
CMD ["main.py"]
