# Reckoner frontend — build the Vite SPA, serve it with nginx which also
# reverse-proxies /api to the backend (single origin; VITE_API_URL left unset
# so the app uses relative /api).
FROM node:22-slim AS build
RUN npm install -g pnpm@9
WORKDIR /app
COPY frontend/package.json frontend/pnpm-lock.yaml /app/
RUN pnpm install --frozen-lockfile || pnpm install
COPY frontend/ /app/
# Build only the SPA (dist/public); the node server bundle is unused (nginx serves).
RUN pnpm exec vite build

FROM nginx:alpine
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist/public /usr/share/nginx/html
EXPOSE 80
