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
FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /srv/dist /usr/share/nginx/html
EXPOSE 80
