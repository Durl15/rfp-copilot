FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /srv
COPY requirements.lock.txt .
RUN pip install -r requirements.lock.txt

COPY app ./app
# imageio-ffmpeg bundles the ffmpeg binary; fail the build if it is missing
RUN python -c "import imageio_ffmpeg, os; p = imageio_ffmpeg.get_ffmpeg_exe(); assert os.path.exists(p), p; print('ffmpeg', p)"

# Cloud Run sets PORT. Its frontend is the only peer, so trust its X-Forwarded-* headers.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
