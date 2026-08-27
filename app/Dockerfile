FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py mcp_server.py ./
COPY static ./static
# default command runs the site; the MCP service overrides it (see compose)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
