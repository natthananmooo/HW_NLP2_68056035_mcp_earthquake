FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY seismic_mcp_server.py .
RUN chmod 644 seismic_mcp_server.py

RUN useradd --create-home appuser
USER appuser

ENV HOST=0.0.0.0 PORT=8000 PYTHONUNBUFFERED=1
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["python", "seismic_mcp_server.py"]
