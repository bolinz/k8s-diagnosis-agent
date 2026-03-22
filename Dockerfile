FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY agent ./agent

RUN pip install --no-cache-dir .

ENTRYPOINT ["k8s-diagnosis-agent"]
CMD ["run"]
