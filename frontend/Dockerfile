# syntax=docker/dockerfile:1

FROM node:22-alpine

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY tsconfig.json next.config.ts eslint.config.mjs vitest.config.ts ./
COPY src ./src
COPY tests ./tests

EXPOSE 3000

CMD ["npm", "run", "dev", "--", "-H", "0.0.0.0"]
