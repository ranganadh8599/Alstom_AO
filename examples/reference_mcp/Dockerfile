FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Pinned: the gateway negotiates a protocol version with the upstream server, so
# a floating SDK could silently change the wire format under it.
RUN pip install "mcp==1.30.0"

WORKDIR /app
COPY app.py /app/app.py

EXPOSE 9102

CMD ["python", "app.py"]
