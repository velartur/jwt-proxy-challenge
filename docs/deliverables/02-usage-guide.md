# Usage guide

## 1. What you get

**jwt-proxy** is an asynchronous HTTP proxy (Python 3.12, aiohttp). It accepts a `POST` on any path,
signs a JSON Web Token with HS512 and forwards the request to the configured upstream with the token
in the `x-my-jwt` header. The upstream response comes back to the caller unchanged. A `/status` page
reports uptime and request counters.

```
            POST /post {"user": "alice"}                 POST /post {"user": "alice"}
 client ───────────────────────────────▶  jwt-proxy  ───────────────────────────────▶ upstream
        ◀───────────────────────────────             ◀─── x-my-jwt: eyJhbGciOiJIUzUxMiJ9... ──
                   upstream response                              upstream response
```

The token carries the claims requested by the challenge:

| claim | value |
|-------|-------|
| `iat` | request timestamp, seconds since the epoch (RFC 7519 §4.1.6) |
| `jti` | 256-bit random nonce, unique per request |
| `payload` | `{"user": "<username>", "date": "<today, UTC, YYYY-MM-DD>"}` |

It is signed with the hex secret from the challenge statement, decoded to its 64 raw bytes.

## 2. Prerequisites

| to run with Docker (recommended) | to run without Docker |
|----------------------------------|-----------------------|
| Docker Engine 27 or 28 with Compose v2 (`docker compose version`) | Python 3.11 or newer (`python3 --version`) |
| GNU make | GNU make |
| outbound internet access to reach the default upstream `postman-echo.com` | same |

Clone the repository and change into it. Every command below is run from the repository root.

## 3. Quick start

```bash
make run                 # builds the image and starts the proxy on http://127.0.0.1:8080
HTTP_PORT=9090 make run  # same, on port 9090
```

The first `make run` copies `.env.example` to `.env`; `.env` holds the signing secret and is
git-ignored. The container stays in the foreground; stop it with `Ctrl+C` or, from another
terminal, `make stop`.

In another terminal, send a request through the proxy:

```bash
curl -s -X POST http://127.0.0.1:8080/post \
     -H 'content-type: application/json' \
     -d '{"user": "alice", "hello": "world"}'
```

By default requests go to `https://postman-echo.com`, which echoes what it received, so the response
shows the header the upstream saw:

```json
{
  "headers": {
    "host": "postman-echo.com",
    "via": "1.1 jwt-proxy",
    "x-my-jwt": "eyJhbGciOiJIUzUxMiIsInR5cCI6IkpXVCJ9.eyJpYXQiOjE3OTUyMzc4MzAsImp0aSI6Ijli...",
    "content-type": "application/json"
  },
  "json": {"user": "alice", "hello": "world"},
  "url": "https://postman-echo.com/post"
}
```

Check the status page:

```bash
curl -s http://127.0.0.1:8080/status
# {"status": "ok", "started_at": "2026-11-21T05:10:10+00:00", "uptime_seconds": 19.7,
#  "requests_processed": 1, "requests_failed": 0, "requests_in_flight": 0}
```

## 4. Inspecting and verifying the token

To read the claims, decode the middle segment of the token (no secret needed):

```bash
python3 -c 'import sys, base64, json
t = sys.argv[1].split(".")
print(json.loads(base64.urlsafe_b64decode(t[1] + "==")))' "<token>"
# {'iat': 1795237830, 'jti': '9bcf6e49cd85...', 'payload': {'user': 'alice', 'date': '2026-11-21'}}
```

To verify the signature with PyJWT (installed in the project's virtualenv or `pip install PyJWT`):

```python
import jwt
secret = bytes.fromhex("a9ddbcaba8c0ac1a0a812dc0c2f08514b23f2db0a68343cb8199ebb38a6d91e4"
                       "ebfb378e22ad39c2d01d0b4ec9c34aa91056862ddace3fbbd6852ee60c36acbf")
print(jwt.get_unverified_header(token))                     # {'alg': 'HS512', 'typ': 'JWT'}
print(jwt.decode(token, secret, algorithms=["HS512"]))      # raises InvalidSignatureError if tampered
```

On <https://jwt.io>, paste the token, tick "secret base64 encoded" and use the base64 form of the
same 64 bytes: `qd28q6jArBoKgS3AwvCFFLI/LbCmg0PLgZnrs4ptkeTr+zeOIq05wtAdC07Jw0qpEFaGLdrOP7vWhS7mDDasvw==`.

## 5. Make targets

| target | what it does |
|--------|--------------|
| `make build` | build the Docker image (`docker compose build proxy`) |
| `make run` | start the proxy; `HTTP_PORT` selects the port (default `8080`) |
| `make test` | run the test suite inside a container built from the `test` stage |
| `make stop` | stop and remove the containers |
| `make logs` | follow the proxy logs |
| `make run-local` | same as `run` with a local Python 3.11+ virtualenv instead of Docker |
| `make test-local` | same as `test` without Docker |
| `make clean` | remove containers, local images, virtualenv and caches |
| `make deploy-ec2` / `tunnel-ec2` / `update-ec2` / `status-ec2` / `destroy-ec2` | AWS deployment (see document 03) |

`make help` prints the same list.

## 6. Configuration

All settings are environment variables; set them in `.env` or in the shell before `make run`.

| variable | default | meaning |
|----------|---------|---------|
| `JWT_SECRET_HEX` | *(required)* | HS512 secret, hex encoded; must decode to at least 64 bytes |
| `UPSTREAM_URL` | `https://postman-echo.com` | base URL; the incoming path and query string are appended |
| `HTTP_PORT` | `8080` | port the proxy listens on (host port when running with Compose) |
| `HTTP_BIND` | `127.0.0.1` | Compose only: host interface to publish on. `0.0.0.0` exposes it on every interface |
| `HTTP_HOST` | `127.0.0.1` | bind address when running without Docker (`0.0.0.0` inside the image) |
| `UPSTREAM_TIMEOUT_SECONDS` | `10` | total timeout for the upstream call; exceeded → `504` |
| `MAX_BODY_BYTES` | `1048576` | largest accepted request body; exceeded → `413` |
| `MAX_RESPONSE_BYTES` | `10485760` | largest accepted upstream response; exceeded → `502` |
| `DEFAULT_USER` | `anonymous` | `payload.user` when the body carries no `user`/`username` field |
| `LOG_LEVEL` | `INFO` | Python log level (`DEBUG` logs per-request upstream timings) |

Examples:

```bash
UPSTREAM_URL=http://host.docker.internal:9000 make run    # your own upstream on the host machine
HTTP_BIND=0.0.0.0 HTTP_PORT=8080 make run                 # reachable from other machines (deliberate)
LOG_LEVEL=DEBUG make run
```

The proxy refuses to start with a missing, non-hex or too-short secret, or an upstream URL that is
not an absolute `http(s)` URL, and says why on stderr (exit code 2).

## 7. Behaviour in detail

* **Only `POST` is proxied.** Any other method on any path gets `405` with `Allow: POST`. `/status`
  is reserved for the proxy itself (`GET`/`HEAD`; `POST /status` is `405`).
* **Path and query are preserved**: `POST /post?x=1` → `UPSTREAM_URL/post?x=1`.
* **Username**: taken from a JSON object body's `user` (or `username`) string field; anything else
  (other content types, arrays, non-JSON, missing field) falls back to `DEFAULT_USER`. The body itself
  is forwarded byte-for-byte whatever its content type.
* **Headers**: hop-by-hop headers (`Connection`, `Keep-Alive`, `Proxy-Authorization`, `TE`,
  `Transfer-Encoding`, `Upgrade`, and anything named in `Connection`) are stripped in both
  directions; `Via: 1.1 jwt-proxy` is appended in both directions; `X-Forwarded-For` is appended on
  the way upstream; a client-supplied `x-my-jwt` is discarded and replaced; `Host` is rewritten to
  the upstream host.
* **Responses pass through unchanged**, including error statuses, redirects (returned, not
  followed) and compressed bodies.
* **Gateway errors**: upstream unreachable → `502 Bad Gateway`; upstream timeout → `504 Gateway
  Timeout`; both are counted in `requests_failed`.
* **`/status`** returns `started_at` (ISO 8601, UTC), `uptime_seconds`, `requests_processed`
  (proxied `POST`s that received a final answer), `requests_failed` and `requests_in_flight`.
  Counters are per process and reset on restart.
* **Logging**: the standard aiohttp access log only; the token and the body are never logged.

Quick checks of each rule:

```bash
# only POST is proxied -> 405, Allow: POST
curl -i http://127.0.0.1:8080/post | head -3

# a client-supplied token is replaced, never forwarded
curl -s -X POST http://127.0.0.1:8080/post -H 'x-my-jwt: forged' -d '{}' \
   | grep -o '"x-my-jwt":"[^"]\{0,40\}'

# non-JSON bodies are forwarded untouched -> "data":"hello"
curl -s -X POST http://127.0.0.1:8080/post -H 'content-type: text/plain' -d 'hello' \
   | grep -o '"data":"[^"]*"'
```

## 8. Tests

```bash
make test         # inside Docker, from the `test` image stage
make test-local   # with a local Python 3.11+ virtualenv
```

45 tests (`pytest` + `pytest-aiohttp`) cover token signing and claims, username extraction,
configuration validation, and the proxy end to end against a fake upstream: forwarding of
method/path/query/body/headers, hop-by-hop stripping, `Via`/`X-Forwarded-For`, pass-through of error
statuses, redirects and gzip bodies, `405`/`413`/`502`/`504` handling and the `/status` counters.
A run takes about one second.

The repository's GitHub Actions workflow runs `make test`, `make build` and a smoke test of the
running container on every push, plus the suite on Python 3.11, 3.12 and 3.13.

## 9. Running without Docker

```bash
make run-local                   # creates .venv with python3 (must be 3.11+) and starts the proxy
HTTP_PORT=9090 make run-local
PYTHON=python3.12 make test-local
```

Or by hand:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
JWT_SECRET_HEX=a9ddbc…acbf python -m jwt_proxy      # or: jwt-proxy (console script)
```

Without Docker the proxy binds to `HTTP_HOST` (default `127.0.0.1`).

## 10. Troubleshooting

| symptom | cause and fix |
|---------|---------------|
| `jwt-proxy: configuration error: JWT_SECRET_HEX is required` | no `.env` and no variable in the shell; `cp .env.example .env` (`make run` does it) |
| `JWT_SECRET_HEX must decode to at least 64 bytes` | the secret is not the full 128-character hex string |
| `error: missing - copy .env.example to .env` from Compose | same as above, raised by Compose before the container starts |
| `bind: address already in use` | the port is taken; `HTTP_PORT=9090 make run` |
| `502 upstream request failed` | the upstream is unreachable from the container (DNS, network, wrong `UPSTREAM_URL`); check `make logs` |
| `504 upstream request timed out` | the upstream took longer than `UPSTREAM_TIMEOUT_SECONDS` |
| `413 Request Entity Too Large` | the body exceeds `MAX_BODY_BYTES` (1 MiB default) |
| `405 Method Not Allowed` | only `POST` is proxied; `/status` accepts `GET`/`HEAD` only |
| the container is `unhealthy` | `/status` is not answering; `make logs` shows why (usually a startup configuration error) |
| `make test` fails with `compose build requires buildx` | install the Docker Buildx plugin (bundled with Docker Desktop; on bare engines install `docker-buildx-plugin`) |

## 11. Where things are

| path | contents |
|------|----------|
| `README.md` | the short version of this guide |
| `src/jwt_proxy/` | application code |
| `tests/` | test suite |
| `Dockerfile`, `docker-compose.yml`, `Makefile`, `.env.example` | packaging and entry points |
| `docs/design.md` | architecture notes; `docs/architecture.drawio` has the diagrams |
| `docs/deploy-ec2.md`, `deploy/ec2/` | AWS deployment |
| `docs/drmtoday-solution-design.md` | solution design for the DRMtoday customer case |
