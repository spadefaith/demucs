FROM --platform=linux/amd64 python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TORCHAUDIO_USE_SOUND_FILE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt

# Install CUDA-enabled PyTorch first, then the rest of the stack. PyPI stays as an extra index so
# torch's own dependencies (and their build backends, e.g. flit_core) still resolve.
RUN pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cu124 \
    --extra-index-url https://pypi.org/simple \
    torch==2.4.1 \
    torchaudio==2.4.1 && \
    pip install --no-cache-dir -r /app/requirements.txt

COPY handler.py /app/handler.py

CMD ["python", "-u", "/app/handler.py"]
