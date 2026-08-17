FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# --proxy-headers so the app sees the real https scheme and hostname behind
# Koyeb's edge, which is what the join URL and the QR code are built from.
# The ws-ping settings keep signalling sockets alive across a quiet classroom;
# the browsers ping too, and between them nothing sits idle long enough for the
# edge to hang up.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*' --ws-ping-interval 20 --ws-ping-timeout 60"]
