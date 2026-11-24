# jwt-proxy

An asynchronous HTTP proxy (Python 3.11+, [aiohttp](https://docs.aiohttp.org/)) that
takes a `POST` request, signs a JSON Web Token with **HS512** and forwards the
request upstream with the token in the `x-my-jwt` header.

```
            POST /post {"user": "alice"}                 POST /post {"user": "alice"}
 client ───────────────────────────────▶  jwt-proxy  ───────────────────────────────▶ upstream
        ◀───────────────────────────────             ◀─── x-my-jwt: eyJhbGciOiJIUzUxMiJ9... ──
                   upstream response                              upstream response
```

The JWT carries the claims requested by the challenge:

| claim     | value                                                        |
|-----------|--------------------------------------------------------------|
| `iat`     | request timestamp, seconds since the epoch (RFC 7519 §4.1.6) |
| `jti`     | 256-bit random nonce from `secrets`, unique per request      |
| `payload` | `{"user": "<username>", "date": "<today, UTC, YYYY-MM-DD>"}` |

The signing secret is the hex string from the challenge, decoded to its 64 raw
bytes (512 bits, the key size RFC 7518 §3.2 requires for HS512).

Repository map: [`src/jwt_proxy/`](src/jwt_proxy) application,
[`tests/`](tests) test suite, [`deploy/ec2/`](deploy/ec2) CloudFormation
deployment, [`docs/design.md`](docs/design.md) architecture and
infrastructure notes, [`docs/deploy-ec2.md`](docs/deploy-ec2.md) AWS
deployment guide, [`docs/drmtoday-solution-design.md`](docs/drmtoday-solution-design.md)
the solution-design part of the challenge (DRMtoday migration proposal),
[`docs/deliverables/pdf/`](docs/deliverables/pdf) the same material as PDF documents,
[`docs/architecture.drawio`](docs/architecture.drawio) the editable diagrams.

## Quick start

Requirements: Docker Engine 27/28 with Compose v2 and `make`. Nothing else.

```bash
make run                 # builds the image and starts the proxy on http://127.0.0.1:8080
HTTP_PORT=9090 make run  # same, on port 9090
```

In another terminal:

```bash
curl -s -X POST http://127.0.0.1:8080/post \
     -H 'content-type: application/json' \
     -d '{"user": "alice", "hello": "world"}'
```

By default requests are forwarded to `https://postman-echo.com`, which echoes
what it received, so the answer shows the header the upstream saw:

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

To inspect the claims, decode the middle segment of the token:

```bash
python3 -c 'import sys,base64,json; t=sys.argv[1].split("."); print(json.loads(base64.urlsafe_b64decode(t[1]+"==")))' "<token>"
# {'iat': 1795237830, 'jti': '9bcf6e49cd85...', 'payload': {'user': 'alice', 'date': '2026-11-21'}}
```

To verify the signature on <https://jwt.io>, paste the token, tick
"secret base64 encoded" and use the secret in base64 form
(`qd28q6jArBoKgS3AwvCFFLI/LbCmg0PLgZnrs4ptkeTr+zeOIq05wtAdC07Jw0qpEFaGLdrOP7vWhS7mDDasvw==`,
which is the same 64 bytes as the hex string). With PyJWT:
`jwt.decode(token, bytes.fromhex(JWT_SECRET_HEX), algorithms=["HS512"])`.

Status page:

```bash
curl -s http://127.0.0.1:8080/status
# {"status": "ok", "started_at": "2026-11-21T05:10:10+00:00", "uptime_seconds": 19.7,
#  "requests_processed": 1, "requests_failed": 0, "requests_in_flight": 0}
```

Stop with `Ctrl+C` or `make stop`.

## Make targets

| target            | what it does                                                        |
|-------------------|---------------------------------------------------------------------|
| `make build`      | build the Docker image (`docker compose build`)                     |
| `make run`        | start the proxy; `HTTP_PORT` selects the port (default `8080`)      |
| `make test`       | run the test suite inside a container built from the `test` stage   |
| `make stop`       | stop and remove the containers                                      |
| `make logs`       | follow the proxy logs                                               |
| `make run-local`  | same as `run` with a local Python 3.11+ virtualenv instead of Docker |
| `make test-local` | same as `test` without Docker                                       |
| `make clean`      | remove containers, local images, virtualenv and caches              |

`make run` creates `.env` from [`.env.example`](.env.example) the first time;
`.env` holds the signing secret and is git-ignored.

## Configuration

All settings are environment variables (set them in `.env` or in the shell).

| variable                   | default                    | meaning                                                              |
|----------------------------|----------------------------|----------------------------------------------------------------------|
| `JWT_SECRET_HEX`           | *(required)*               | HS512 secret, hex encoded; must decode to at least 64 bytes          |
| `UPSTREAM_URL`             | `https://postman-echo.com` | base URL; the incoming path and query string are appended            |
| `HTTP_PORT`                | `8080`                     | port the proxy listens on (host port when running with Compose)      |
| `HTTP_BIND`                | `127.0.0.1`                | Compose only: host interface to publish on. `0.0.0.0` = all          |
| `HTTP_HOST`                | `127.0.0.1`                | bind address when running without Docker (`0.0.0.0` inside the image) |
| `UPSTREAM_TIMEOUT_SECONDS` | `10`                       | total timeout for the upstream call; exceeded → `504`                |
| `MAX_BODY_BYTES`           | `1048576`                  | largest accepted request body; exceeded → `413`                      |
| `MAX_RESPONSE_BYTES`       | `10485760`                 | largest accepted upstream response; exceeded → `502`                 |
| `DEFAULT_USER`             | `anonymous`                | `payload.user` when the body carries no `user`/`username` field      |
| `LOG_LEVEL`                | `INFO`                     | Python log level                                                     |

The proxy refuses to start with a missing, non-hex or too-short secret, or an
upstream URL that is not absolute `http(s)`.

## Behaviour in detail

* **Only `POST` is proxied.** Any other method on any path gets `405` with
  `Allow: POST`. `/status` is reserved for the proxy itself (`GET`/`HEAD`).
* **Path and query are preserved**: `POST /post?x=1` → `UPSTREAM_URL/post?x=1`.
* **Username**: taken from a JSON object body's `user` (or `username`) string
  field; anything else falls back to `DEFAULT_USER`. The body itself is
  forwarded byte-for-byte, whatever its content type.
* **Headers** (RFC 2616 / RFC 7230): hop-by-hop headers (`Connection`,
  `Keep-Alive`, `Proxy-Authorization`, `TE`, `Transfer-Encoding`, `Upgrade`,
  ... and anything named in `Connection`) are stripped in both directions;
  `Via: 1.1 jwt-proxy` is appended in both directions; `X-Forwarded-For` is
  appended on the way upstream; a client-supplied `x-my-jwt` is discarded and
  replaced. `Host` is rewritten to the upstream host.
* **Responses pass through unchanged**, including error statuses, redirects
  (not followed) and compressed bodies (`auto_decompress` is off, so bytes
  and `Content-Encoding` stay consistent).
* **Gateway errors**: connection failures → `502 Bad Gateway`, timeouts →
  `504 Gateway Timeout`; both are counted in `requests_failed`.
* **`/status`** reports `started_at`, `uptime_seconds`, `requests_processed`
  (proxied POSTs that received a final answer), `requests_failed` and
  `requests_in_flight`. Counters are per process.
* **Logging**: the standard access log only. The token and the body are never
  logged.

## Tests

```bash
make test         # inside Docker
make test-local   # local Python 3.11+
```

45 tests (`pytest` + `pytest-aiohttp`) cover token signing and claims,
username extraction, configuration validation, and the proxy end to end
against a fake upstream: forwarding of method/path/query/body/headers,
hop-by-hop stripping, `Via`/`X-Forwarded-For`, pass-through of error
statuses, redirects and gzip bodies, `405`/`413`/`502`/`504` handling and the
`/status` counters.

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs `make test`,
`make build` and a smoke test of the running container on every push, plus
the suite on Python 3.11/3.12/3.13.

## Deploying to AWS (one EC2 instance)

```bash
make deploy-ec2     # CloudFormation: t4g.micro + Docker Compose, no inbound ports
make tunnel-ec2     # http://localhost:8080 -> the instance over SSM Session Manager
make destroy-ec2
```

Needs AWS credentials and the Session Manager plugin for the tunnel. The code
is cloned from the `origin` remote, or shipped through a private S3 bucket
when the checkout has none. Accounts with VPC Block Public Access get a
private subnet + NAT gateway automatically. Details, access options and costs
in [`docs/deploy-ec2.md`](docs/deploy-ec2.md).

## Running without Docker

```bash
make run-local                   # creates .venv with python3 (must be 3.11+), starts the proxy
HTTP_PORT=9090 make run-local
PYTHON=python3.12 make test-local
```

Or by hand: `pip install -e '.[test]'`, then `JWT_SECRET_HEX=... python -m jwt_proxy`.

## Design decisions and assumptions

* **aiohttp** for both server and client: one dependency gives an async HTTP
  server, a pooled async client and test utilities; a proxy needs low-level
  header control more than a web framework.
* **Reverse-proxy semantics** (fixed upstream, incoming path appended) rather
  than a forward proxy with absolute URIs: that is what "append a header to the
  upstream POST" calls for, and it is what runs in front of real services.
* **Secret from the environment, never from code.** The challenge value lives
  only in `.env.example`; the app fails fast without a valid key.
* **`user` comes from the request body** because the request is the only
  context the proxy has. In production the identity would come from the
  authenticated caller (mTLS, API key, session) and not from untrusted input.
* **`date` is the UTC calendar date** of the request, ISO 8601.
* **Private by default**: the published port binds to `127.0.0.1` on the host
  and the bare-metal default bind is loopback. Exposing it is one variable
  (`HTTP_BIND=0.0.0.0`) and a deliberate choice.
* **Responses are buffered** (capped by `MAX_RESPONSE_BYTES`) so every
  upstream failure maps to a correct status code. Streaming would be the next
  step if large payloads were expected.

More in [`docs/design.md`](docs/design.md).
