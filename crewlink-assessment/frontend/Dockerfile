# Build: install + next build (standalone output)
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
# Rewrites are fixed at build time, so the API address is a build arg.
ARG API_URL=http://api:8000
ENV API_URL=$API_URL STANDALONE=1 NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# Run: just the standalone server and static files, no node_modules
FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
USER node
EXPOSE 3000
CMD ["node", "server.js"]
