FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /bin/uv
WORKDIR /app
RUN useradd --create-home --uid 10001 app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
# Dependencies first (cached until uv.lock changes), then the project itself.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project --extra postgres
COPY src ./src
RUN uv sync --locked --no-dev --no-editable --extra postgres \
    && mkdir -p /app/.data && chown app /app/.data
ENV PATH="/app/.venv/bin:$PATH"
USER app
ENTRYPOINT ["nexo"]
