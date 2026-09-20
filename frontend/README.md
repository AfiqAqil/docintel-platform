# Frontend

React 18, TypeScript, Vite. No CSS framework, no UI kit, no state library.

## Develop

```
npm install
npm run dev
```

The dev server proxies `/api` to `http://localhost:8000`, so it can talk to a
locally running backend without CORS.

## Build

```
npm run build
```

Runs `tsc --noEmit` before `vite build`, so a type error fails the build
instead of shipping. `npm run typecheck` runs just the type check.
`npm run preview` serves the built output locally.

## nginx

The SPA is served in every deployed environment by
`nginxinc/nginx-unprivileged`, listening on **8080**, not 80, because that
image cannot bind a privileged port as a non-root user, and the ALB target
group points at 8080.

The config lives at `nginx/templates/default.conf.template`. The image's own
entrypoint runs `envsubst` over it at container start, so no custom
entrypoint is needed. Two variables must be set:

| Variable | AWS | docker compose |
|---|---|---|
| `DNS_RESOLVER` | `169.254.169.253` (VPC DNS) | `127.0.0.11` (Docker's embedded DNS) |
| `API_UPSTREAM` | `api.docintel.internal:8000` (Cloud Map private DNS name) | the backend service's compose name and port, e.g. `backend:8000` |

The resolver is a variable and not a literal because the two environments use
different DNS servers. The upstream uses a variable in `proxy_pass`
(`set $upstream ...; proxy_pass http://$upstream;`) rather than a bare
hostname, which is what forces nginx to re-resolve on the 10 second cycle set
by `resolver ... valid=10s` instead of resolving once at startup and pinning
itself to an ECS task that no longer exists after a deploy. See
`docs/ARCHITECTURE.md` section 8 for the full reasoning.
