# KnowFlow backend — multi-stage build.
FROM node:22-slim AS build
WORKDIR /srv
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json ./
COPY src ./src
RUN npm run build

# Runtime image — lean, no toolchain.
FROM node:22-slim AS runtime
ENV NODE_ENV=production
WORKDIR /srv
COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY --from=build /srv/dist ./dist
# Fresh-DB bootstrap (see migrate.mjs): base schema + incremental deltas.
COPY migrate.mjs ./migrate.mjs
COPY bootstrap.sql ./bootstrap.sql
COPY src/database/migrations ./migrations
EXPOSE 3000
CMD ["node", "dist/server.js"]
