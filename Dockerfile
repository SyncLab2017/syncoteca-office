FROM python:3.11-slim

WORKDIR /app

# System deps for pdfplumber, python-docx, pydub
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY src/ src/

# Install project + deps
RUN pip install --no-cache-dir -e .

# Knowledge data and prompts
COPY data/knowledge/ data/knowledge/
COPY src/syncoteca/config/prompts/ src/syncoteca/config/prompts/

COPY start.py .

ENV PYTHONUNBUFFERED=1

CMD ["python", "start.py"]
