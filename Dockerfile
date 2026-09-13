FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/harmon

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web

ENV HARMON_DB=/config/harmon.db \
    PYTHONUNBUFFERED=1

VOLUME ["/config", "/music", "/originals"]
EXPOSE 8730

CMD ["uvicorn", "app.main:api", "--host", "0.0.0.0", "--port", "8730"]
