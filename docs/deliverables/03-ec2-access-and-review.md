# AWS deployment: access and review guide

## 1. What is deployed

The proxy runs on one EC2 instance created by the CloudFormation stack described in document 01,
section 8. The instance has **no public IP and no inbound security-group rules**; every interaction
goes through AWS Systems Manager (SSM) Session Manager, authenticated with your AWS credentials.

| item | value |
|------|-------|
| AWS account | `{{ACCOUNT_ID}}` |
| Region | `{{REGION}}` |
| AWS CLI profile used | `{{PROFILE}}` |
| CloudFormation stack | `{{STACK_NAME}}` ({{STACK_STATUS}}) |
| EC2 instance | `{{INSTANCE_ID}}` — `{{INSTANCE_TYPE}}`, Amazon Linux 2023 (arm64), launched {{LAUNCHED_AT}} |
| Network | `{{NETWORK_MODE}}` mode: private IP `{{PRIVATE_IP}}` in `{{SUBNET_ID}}` ({{AVAILABILITY_ZONE}}), outbound through NAT gateway `{{NAT_GATEWAY_ID}}` |
| Deployed source | `{{SOURCE}}` (commit `{{SOURCE_COMMIT}}`) |
| Proxy endpoint on the instance | `http://127.0.0.1:8080` (container port published on loopback only) |
| Upstream | `https://postman-echo.com` |

> Live values in this table are read from the account when the document is built with
> `python docs/deliverables/build.py --live`. Check the current state at any time with
> `make status-ec2`.

## 2. Prerequisites on your machine

1. **AWS CLI v2** with credentials for the account (refresh your federated/SSO session first if it
   has expired):
   ```bash
   export AWS_PROFILE={{PROFILE}}
   aws sts get-caller-identity   # must show account {{ACCOUNT_ID}}
   ```
2. **Session Manager plugin** for the AWS CLI (needed for the tunnel and the shell), on your `PATH`:
   ```bash
   session-manager-plugin --version     # 1.2.x
   # if missing: brew install --cask session-manager-plugin   (macOS)
   # other platforms: https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html
   ```
3. A checkout of the repository, for the `make` targets:
   ```bash
   cd <path-to-your-checkout>/jwt-proxy-challenge
   ```

## 3. Three ways in

### 3.1 Use the proxy from your laptop (SSM port forwarding)

In one terminal, keep the tunnel open:

```bash
make tunnel-ec2
# Forwarding http://localhost:8080 -> i-… :8080  (Ctrl+C to stop)
```

In another terminal, `http://localhost:8080` is now the proxy on the instance:

```bash
curl -s http://localhost:8080/status
curl -s -X POST http://localhost:8080/post -H 'content-type: application/json' -d '{"user":"alice"}'
```

Under the hood: `aws ssm start-session --target {{INSTANCE_ID}} --document-name AWS-StartPortForwardingSession --parameters '{"portNumber":["8080"],"localPortNumber":["8080"]}'`.
No port is opened on the instance; the connection rides the SSM agent's outbound TLS channel.

### 3.2 Shell on the instance (no SSH)

```bash
deploy/ec2/deploy.sh shell          # = aws ssm start-session --target {{INSTANCE_ID}}
```

You land as `ssm-user`. Switch to root to use Docker:

```bash
sudo -i
cd /opt/jwt-proxy
```

Leave with `exit` (root) and `exit` (session). Alternatively, open the session in the browser from
the Systems Manager console (Session Manager → Start session → pick the instance).

### 3.3 AWS console

Sign in to account `{{ACCOUNT_ID}}` and open:

* **EC2 → Instances → `{{INSTANCE_ID}}`**: note "Public IPv4 address: –", the private subnet, and
  under *Security* a security group with no inbound rules.
* **CloudFormation → Stacks → `{{STACK_NAME}}`**: the *Resources*, *Outputs* (instance id, ready-made
  SSM commands) and *Template* tabs.
* **Systems Manager → Session Manager**: browser-based shell on the instance.

## 4. Review checklist

Run these on the instance (section 3.2, as root in `/opt/jwt-proxy`). The same `curl` commands work
from the laptop through the tunnel (section 3.1) by replacing `127.0.0.1` with `localhost`.

### 4.1 The proxy is up and healthy

```bash
docker compose ps
# NAME                IMAGE              STATUS                    PORTS
# jwt-proxy-proxy-1   jwt-proxy:latest   Up 2 hours (healthy)      127.0.0.1:8080->8080/tcp

curl -s http://127.0.0.1:8080/status; echo
# {"status": "ok", "started_at": "…", "uptime_seconds": …, "requests_processed": N,
#  "requests_failed": 0, "requests_in_flight": 0}
```

`healthy` comes from the image's `HEALTHCHECK`, which calls `/status` every 30 seconds.

### 4.2 A POST goes through and receives the JWT

```bash
curl -s -X POST http://127.0.0.1:8080/post \
     -H 'content-type: application/json' \
     -d '{"user":"alice"}' | python3 -m json.tool
```

postman-echo returns what it received. Look for, under `headers`:

* `"via": "1.1 jwt-proxy"` — the proxy announced itself (RFC 2616 §14.45);
* `"x-my-jwt": "eyJhbGciOiJIUzUxMiIs…"` — the token the proxy minted and attached;
* `"x-forwarded-for": "127.0.0.1"` — the client address as seen by the proxy;

and under `json` the original body, forwarded untouched.

### 4.3 The JWT is valid: HS512 and the three claims

The container's Python has PyJWT and the secret in its environment, so the signature can be verified
there without copying the secret anywhere:

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8080/post \
          -H 'content-type: application/json' -d '{"user":"alice"}' \
        | python3 -c 'import sys, json; print(json.load(sys.stdin)["headers"]["x-my-jwt"])')

docker exec jwt-proxy-proxy-1 python -c "
import os, sys, jwt
tok = sys.argv[1]; secret = bytes.fromhex(os.environ['JWT_SECRET_HEX'])
print('header:', jwt.get_unverified_header(tok))
print('claims:', jwt.decode(tok, secret, algorithms=['HS512']))
" "$TOKEN"
# header: {'alg': 'HS512', 'typ': 'JWT'}
# claims: {'iat': 1795321582, 'jti': '773151e9…', 'payload': {'user': 'alice', 'date': '2026-11-22'}}
```

`jwt.decode` raises `InvalidSignatureError` if the token was not signed with this secret, and
`InvalidAlgorithmError` if `algorithms` does not include the token's algorithm, so a clean print is
proof of both the signature and the HS512 algorithm.

### 4.4 Counters move and requests are logged

```bash
curl -s http://127.0.0.1:8080/status; echo          # requests_processed increased by the POSTs above
docker compose logs --tail 20 proxy                 # one access-log line per request, status 200
```

The access log carries method, path, status and size only; the token never appears in it.

### 4.5 Proxy rules

```bash
# only POST is proxied -> HTTP/1.1 405 Method Not Allowed, Allow: POST
curl -si http://127.0.0.1:8080/post | head -3

# a client-supplied token is discarded and replaced by a real one
curl -s -X POST http://127.0.0.1:8080/post -H 'x-my-jwt: forged' -d '{}' \
   | grep -o '"x-my-jwt":"[^"]\{0,40\}'

# path and query string are preserved -> "url":"https://postman-echo.com/post?x=1"
curl -s -X POST 'http://127.0.0.1:8080/post?x=1' -d '{}' | grep -o '"url":"[^"]*"'

# non-JSON bodies are forwarded untouched -> "data":"hello"
curl -s -X POST http://127.0.0.1:8080/post -H 'content-type: text/plain' -d 'hello' \
   | grep -o '"data":"[^"]*"'
```

### 4.6 Container and host hardening

```bash
docker exec jwt-proxy-proxy-1 id
# uid=10001(jwtproxy) gid=999(jwtproxy) groups=999(jwtproxy)     <- not root

docker inspect jwt-proxy-proxy-1 --format 'readonly={{.HostConfig.ReadonlyRootfs}} \
  caps={{.HostConfig.CapDrop}} ports={{json .HostConfig.PortBindings}}'
# readonly=true caps=[ALL] ports={"8080/tcp":[{"HostIp":"127.0.0.1","HostPort":"8080"}]}

cat .env          # JWT_SECRET_HEX, UPSTREAM_URL, HTTP_PORT, HTTP_BIND=127.0.0.1
ls -l .env        # -rw------- root root   (written with umask 077 at first boot)
ss -ltnp          # 127.0.0.1:8080 is the only service port; no sshd
```

From the laptop, confirm the network posture with the AWS CLI:

```bash
aws ec2 describe-instances --instance-ids {{INSTANCE_ID}} \
    --query 'Reservations[].Instances[].[PublicIpAddress,PrivateIpAddress,SubnetId]' --output text
# None    {{PRIVATE_IP}}    {{SUBNET_ID}}        <- no public IP

SG=$(aws ec2 describe-instances --instance-ids {{INSTANCE_ID}} \
     --query 'Reservations[].Instances[].SecurityGroups[0].GroupId' --output text)
aws ec2 describe-security-group-rules --filters Name=group-id,Values=$SG \
    --query "SecurityGroupRules[?IsEgress==\`false\`]" --output text
# (empty)                                                   <- no inbound rules
```

### 4.7 How the instance was built

```bash
less /var/log/cloud-init-output.log   # the whole first boot: packages, Compose/Buildx, S3 fetch, build
ls /opt/jwt-proxy                     # the deployed source tree (Dockerfile, compose file, src/, tests/)
```

## 5. Updating, cost and teardown

| action | command | effect |
|--------|---------|--------|
| ship the latest local commit | `make update-ec2` | uploads `git archive HEAD` to the private bucket, then `docker compose up -d --build` on the instance through SSM Run Command; a few seconds of downtime |
| show stack status and outputs | `make status-ec2` | instance id, network mode, ready-made SSM commands |
| change secret, upstream or branch | `make destroy-ec2 && make deploy-ec2` | these are first-boot inputs; recreate the instance (or edit `/opt/jwt-proxy/.env` by hand and `docker compose up -d`) |
| **delete everything** | `make destroy-ec2` | removes instance, NAT gateway, Elastic IP, private subnet, route table, security group and IAM role |

**Cost while it runs** (private-nat mode): about USD 1.3 per day — roughly USD 33 per month for the
NAT gateway, 6 for the `t4g.micro`, under 1 for the volume, plus USD 0.045 per GB through the NAT.
The source bucket costs nothing measurable and its objects expire after 7 days. When the review is
over, `make destroy-ec2` stops all charges.

## 6. Troubleshooting

| symptom | what to do |
|---------|------------|
| `ExpiredToken` or a credential error from the AWS CLI | refresh your federated/SSO session, confirm `aws sts get-caller-identity`, then retry |
| `SessionManagerPlugin is not found` | add `~/.local/bin` to `PATH` or install the plugin (section 2) |
| `TargetNotConnected` from SSM | the agent takes a minute after boot; otherwise check `aws ssm describe-instance-information` and that the instance is `running` |
| `Port 8080 opened for sessionId …` but `curl localhost:8080` fails | another local process owns 8080; stop it or edit the `localPortNumber` in `deploy/ec2/deploy.sh` |
| `/status` returns nothing on the instance | `docker compose ps`; if the container is restarting, `docker compose logs proxy` shows the configuration error |
| stack in `CREATE_FAILED` after a redeploy | the first boot did not reach `/status` in 10 minutes; open a shell and read `/var/log/cloud-init-output.log`; delete the stack before deploying again |
| a VPC Block Public Access notification arrives | expected only if an instance with a public IP was launched; the current deployment has none, and nothing in the VPC needs inbound internet access |

## 7. Security notes for the reviewer

* The instance has no public IP and its security group accepts no inbound traffic; the only paths in
  are SSM sessions authenticated by IAM. There are no SSH keys and no SSH daemon in use.
* The IAM role grants `AmazonSSMManagedInstanceCore` and read access to this stack's own prefix in
  the source bucket, nothing more. IMDSv2 is enforced.
* The signing secret was passed to CloudFormation as a `NoEcho` parameter, written to a root-only
  `.env` with shell tracing disabled, and injected into the container as an environment variable.
  It does not appear in logs, in the image, or in the repository (`.env` is git-ignored; only
  `.env.example` carries the challenge's public value).
* The account's VPC Block Public Access guardrail stays untouched: the deployment adapts to it with
  a private subnet and a NAT gateway instead of requesting an exclusion.
