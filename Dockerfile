FROM debian:12-slim AS build
RUN --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,target=/var/cache/apt,sharing=locked \
    apt-get update && \
    apt-get install --no-install-suggests --no-install-recommends -y \
      curl \
      python3-dev \
      pkg-config \
      libssl-dev \
      libffi-dev \
      ca-certificates && \
    update-ca-certificates
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    /root/.local/bin/uv venv /venv
ENV PATH="/root/.local/bin:${PATH}"
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv pip install --link-mode=copy --python /venv/bin/python --upgrade pip setuptools wheel && \
    uv pip install --link-mode=copy --python /venv/bin/python -r requirements.txt

FROM gcr.io/distroless/python3-debian12
COPY --from=build /venv /venv
COPY . /app/
WORKDIR /app
ENV PYTHONPATH=/app

ENTRYPOINT ["/venv/bin/python3"]