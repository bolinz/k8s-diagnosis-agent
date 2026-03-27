FROM node:20-alpine AS web-build

WORKDIR /web

COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY agent ./agent
COPY --from=web-build /agent/ui/frontend_dist ./agent/ui/frontend_dist

RUN pip install --no-cache-dir .

ENTRYPOINT ["k8s-diagnosis-agent"]
CMD ["run"]
