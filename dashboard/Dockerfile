FROM node:22-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
ARG NEXT_PUBLIC_OMS_API_URL
RUN npm run build

FROM node:22-slim
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
RUN useradd --create-home --uid 10001 web
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
USER web
EXPOSE 3000
CMD ["node", "server.js"]
