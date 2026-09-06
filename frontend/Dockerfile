# KnowFlow frontend — build the Vite SPA, serve via nginx.
FROM node:22-slim AS build
WORKDIR /srv
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json tsconfig.app.json tsconfig.node.json index.html vite.config.ts ./
COPY public ./public
COPY src ./src
RUN npm run build

# Runtime — static files + proxy to the backend API.
# PORT/BACKEND_URL are injected at container start by the nginx entrypoint
# (envsubst over nginx.conf.template). Defaults keep `docker compose` working
# unchanged; Render overrides both via service environment variables.
FROM nginx:1.27-alpine
ENV PORT=80 \
    BACKEND_URL=http://backend:3000
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /srv/dist /usr/share/nginx/html
EXPOSE 80