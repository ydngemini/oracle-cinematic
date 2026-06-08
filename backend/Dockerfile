FROM python:3.12-slim

# Build argument allows pinning a custom pip index (e.g. a private PyPI mirror).
ARG PIP_INDEX_URL=https://pypi.org/simple

WORKDIR /app

# Install dependencies in a separate layer so rebuilds skip this step when
# only source code changes (requirements.txt changes less often than .py files).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the backend source. The compose dev setup bind-mounts /app
# over this, so this layer is only meaningful in a production image build.
COPY . .

# Non-root runtime user — the app never needs to write outside /app.
RUN useradd -r -u 1001 oracle && chown -R oracle:oracle /app
USER oracle

EXPOSE 8000

# Production default. docker-compose.yml overrides this to add --reload.
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
