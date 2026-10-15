# syntax=docker/dockerfile:1
#
# A node app built the way most of them are: install, build, test, run. It
# builds and the image works, which is the problem -- five of these lines do
# nothing and the build is green either way.

ARG NODE_VERSION=20
ARG APP_ENV=production
ARG BUILD_DATE

FROM node:${NODE_VERSION}-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

FROM node:${NODE_VERSION}-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NODE_ENV=${APP_ENV}
RUN npm run build

FROM node:${NODE_VERSION}-alpine AS test
WORKDIR /app
COPY --from=build /app ./
RUN npm test

FROM node:${NODE_VERSION}-alpine AS runtime
WORKDIR /app
ENV PORT=3000
ENV PORT=8080
COPY --from=builder /app/dist ./dist
COPY --from=deps /app/node_modules ./node_modules
EXPOSE ${PORT}
USER node
ENTRYPOINT ["node", "dist/server.js"]
