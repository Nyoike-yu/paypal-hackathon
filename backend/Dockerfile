# Stash backend (FastAPI). Build from the folder that holds main.py, requirements.txt, schema.sql.
FROM python:3.12-slim

# Node.js is only here for the optional PayPal MCP server (`npx @paypal/mcp`). Drop these two lines
# and set PAYPAL_MCP=off if you don't want it; the app then uses the PayPal REST API directly.
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install PayPal's MCP server in its own folder. Its `mcp` command would clash with the Python `mcp` package's script.
RUN npm install --prefix /opt/paypal-mcp @paypal/mcp@1.8.1
ENV PAYPAL_MCP_COMMAND="node /opt/paypal-mcp/node_modules/@paypal/mcp/dist/index.js --tools=orders.get"

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY *.py schema.sql ./

# Run as a normal user. npx needs a writable cache, so point it at the user's home.
RUN useradd --create-home app && chown -R app /app
USER app
ENV NPM_CONFIG_CACHE=/home/app/.npm

EXPOSE 8000

# ONE worker on purpose: background sweeps, the PayPal MCP session and the push pool live inside the
# process, so more workers would run every sweep twice. Scale with a bigger machine, not --workers.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
