FROM python:3.12-slim

RUN useradd -m -u 1000 jobhunt
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY jobhunt/ ./jobhunt/
COPY config.yaml companies.yaml ./

# The database lives on a mounted volume so it survives redeploys.
ENV JOBHUNT_DB=/data/jobs.db
RUN mkdir -p /data && chown jobhunt:jobhunt /data
VOLUME ["/data"]
USER jobhunt

EXPOSE 8000

# Scheduler + web console in one process. The scheduler persists its
# last-run dates in the database, so restarts never double-send.
CMD ["python", "-m", "jobhunt", "--db", "/data/jobs.db", "daemon", "--with-web", "--host", "0.0.0.0", "--port", "8000"]
