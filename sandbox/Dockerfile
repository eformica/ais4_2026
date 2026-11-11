# Sandbox de execução dos agentes (D10, spec seção 7): imagem fixada por tag + digest.
# Build: docker build -t ais4-sandbox:0.1.0 sandbox/
FROM python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib

# Versões e hashes exatos (gerado por `uv pip compile sandbox/requirements.in`).
COPY requirements.lock /tmp/requirements.lock
RUN pip install --require-hashes --only-binary=:all: -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock

WORKDIR /work
