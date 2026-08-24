FROM public.ecr.aws/docker/library/node:20-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0

WORKDIR /app

COPY package.json ./
RUN npm install --production

COPY src/ src/

RUN groupadd -r appgroup && useradd -r -g appgroup appuser
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["node", "-e", "require('http').get('http://localhost:8083/health', r => r.statusCode === 200 ? process.exit(0) : process.exit(1))"]

EXPOSE 8083

CMD ["node", "src/index.js"]
