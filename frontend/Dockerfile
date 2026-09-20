# syntax=docker/dockerfile:1

FROM node:22-slim AS builder

WORKDIR /build

# npm ci, not npm install, so the build uses the locked versions rather than resolving
# fresh ones. Copied before the source for the same layer caching reason as the Python images.
COPY package.json package-lock.json ./
RUN npm ci

COPY tsconfig.json tsconfig.node.json vite.config.ts index.html ./
COPY src ./src
# The build script runs tsc --noEmit first, so a type error fails the image build rather
# than shipping.
RUN npm run build


# nginx-unprivileged rather than nginx: it runs as a non root user out of the box, which is
# why the server listens on 8080 rather than 80. The ALB target group points at 8080.
FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime

# The image's own entrypoint runs envsubst over everything in templates/ at container start,
# so DNS_RESOLVER and API_UPSTREAM are substituted without a custom entrypoint script.
COPY nginx/templates/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=builder /build/dist /usr/share/nginx/html

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/healthz >/dev/null || exit 1
