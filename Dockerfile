FROM python:3.11-slim

# Install timezone data — required for APScheduler to use America/New_York
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && \
    rm -rf /var/lib/apt/lists/*
ENV TZ=America/New_York

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8888
CMD ["sh", "-c", "python dashboard.py & python main.py"]
