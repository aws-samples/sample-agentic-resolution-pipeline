FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

WORKDIR /app

RUN pip install --no-cache-dir boto3 botocore

COPY worker.py .

RUN useradd -r -s /bin/false worker
USER worker

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["python", "-c", "import sys; sys.exit(0)"]

ENTRYPOINT ["python", "-u", "/app/worker.py"]
