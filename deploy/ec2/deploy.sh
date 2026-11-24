#!/usr/bin/env bash
# Deploy jwt-proxy to a single EC2 instance with CloudFormation.
#
#   deploy/ec2/deploy.sh up       create or update the stack (default)
#   deploy/ec2/deploy.sh status   show stack status and outputs
#   deploy/ec2/deploy.sh tunnel   forward http://localhost:8080 to the instance (SSM, no inbound ports)
#   deploy/ec2/deploy.sh shell    interactive shell on the instance (SSM)
#   deploy/ec2/deploy.sh update   git pull + docker compose up --build on the instance (SSM)
#   deploy/ec2/deploy.sh down     delete the stack
#
# Credentials and region come from the AWS CLI as usual (AWS_PROFILE, AWS_REGION, ...).
# Tunables (environment variables, all optional):
#   STACK_NAME      default jwt-proxy
#   REPO_URL        default: origin remote of this checkout, as https
#   GIT_REF         default: current branch
#   JWT_SECRET_HEX  default: value from ./.env
#   UPSTREAM_URL    default https://postman-echo.com
#   HTTP_PORT       default 8080
#   INSTANCE_TYPE   default t4g.micro
#   ALLOWED_CIDR    default empty = SSM access only; e.g. 203.0.113.7/32 to open the port to one address
#   VPC_ID/SUBNET_ID default: the account's default VPC and one of its subnets
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
STACK_NAME="${STACK_NAME:-jwt-proxy}"
HTTP_PORT="${HTTP_PORT:-8080}"

die() { echo "deploy.sh: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 is required: $2"; }

need aws "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
aws sts get-caller-identity --output text --query Arn >/dev/null 2>&1 \
  || die "no valid AWS credentials (set AWS_PROFILE / log in first)"

output() {  # output <OutputKey>
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
      --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

instance_id() {
  local id; id="$(output InstanceId)"
  [[ -n "$id" && "$id" != "None" ]] || die "stack $STACK_NAME has no InstanceId output; is it deployed?"
  echo "$id"
}

# Without a git remote, ship the committed tree as a tarball through a private
# bucket of the account (block-public-access on, objects expire after 7 days).
upload_source() {  # prints "<bucket> <key>"
  local account region bucket key commit tarball
  account="$(aws sts get-caller-identity --query Account --output text)"
  region="$(aws configure get region 2>/dev/null || echo "${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}")"
  bucket="jwt-proxy-src-${account}-${region}"
  if ! aws s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
    if [[ "$region" == "us-east-1" ]]; then
      aws s3api create-bucket --bucket "$bucket" >/dev/null
    else
      aws s3api create-bucket --bucket "$bucket" --create-bucket-configuration LocationConstraint="$region" >/dev/null
    fi
    aws s3api put-public-access-block --bucket "$bucket" --public-access-block-configuration \
        BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true >/dev/null
    aws s3api put-bucket-lifecycle-configuration --bucket "$bucket" --lifecycle-configuration \
        '{"Rules":[{"ID":"expire-old-tarballs","Status":"Enabled","Filter":{"Prefix":""},"Expiration":{"Days":7}}]}' >/dev/null
  fi
  commit="$(git -C "$root" rev-parse --short HEAD)"
  key="${STACK_NAME}/${commit}.tgz"
  tarball="$(mktemp -t jwt-proxy-src.XXXXXX)"
  git -C "$root" archive --format=tar HEAD | gzip > "$tarball"
  aws s3 cp --quiet "$tarball" "s3://${bucket}/${key}"
  rm -f "$tarball"
  echo "$bucket $key"
}

# First /24 in 172.31.250.0-255.0 (top of the default-VPC range) that no subnet uses.
free_private_cidr() {  # free_private_cidr <vpc-id>
  local vpc_id="$1" used n cidr
  used="$(aws ec2 describe-subnets --filters Name=vpc-id,Values="$vpc_id" --query 'Subnets[].CidrBlock' --output text)"
  for n in 250 251 252 253 254 255; do
    cidr="172.31.$n.0/24"
    grep -qF -- "$cidr" <<<"$used" || { echo "$cidr"; return; }
  done
  die "no free /24 found in 172.31.250-255; set PRIVATE_SUBNET_CIDR"
}

cmd_up() {
  local repo_url git_ref secret vpc_id subnet_id source_bucket="" source_key=""
  repo_url="${REPO_URL:-$(git -C "$root" remote get-url origin 2>/dev/null || true)}"
  if [[ -n "$repo_url" ]]; then
    # git@github.com:user/repo.git -> https://github.com/user/repo.git
    repo_url="$(sed -E 's#^git@([^:]+):#https://\1/#' <<<"$repo_url")"
    git_ref="${GIT_REF:-$(git -C "$root" rev-parse --abbrev-ref HEAD)}"
    # A remote that does not (yet) have the ref is useless to the instance: fall back to S3.
    if ! git ls-remote --exit-code --heads --tags "$repo_url" "$git_ref" >/dev/null 2>&1; then
      echo "Source: $repo_url has no '$git_ref' yet (not pushed?); falling back to S3"
      repo_url=""
    fi
  fi
  if [[ -n "$repo_url" ]]; then
    echo "Source: $repo_url @ $git_ref (git clone on the instance)"
  else
    git_ref=""
    echo "Source: no git remote, uploading the committed tree (HEAD) to a private S3 bucket"
    read -r source_bucket source_key <<<"$(upload_source | tail -n 1)"
    echo "Source: s3://$source_bucket/$source_key"
  fi

  secret="${JWT_SECRET_HEX:-$(sed -n 's/^JWT_SECRET_HEX=//p' "$root/.env" 2>/dev/null || true)}"
  [[ -n "$secret" ]] || die "JWT_SECRET_HEX is not set and ./.env has no value (cp .env.example .env)"

  vpc_id="${VPC_ID:-$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)}"
  [[ -n "$vpc_id" && "$vpc_id" != "None" ]] || die "no default VPC found; set VPC_ID and SUBNET_ID"
  subnet_id="${SUBNET_ID:-$(aws ec2 describe-subnets --filters Name=vpc-id,Values="$vpc_id" Name=map-public-ip-on-launch,Values=true \
               --query 'Subnets[0].SubnetId' --output text)}"
  [[ -n "$subnet_id" && "$subnet_id" != "None" ]] || die "no public subnet found in $vpc_id; set SUBNET_ID"

  # VPC Block Public Access in "block-ingress"/"block-bidirectional" mode also
  # stops instances with public IPs from *initiating* connections through an
  # internet gateway; only NAT gateways are allowed. Detect it and switch to
  # a private subnet + NAT gateway unless NETWORK_MODE says otherwise.
  local network_mode bpa_mode az private_cidr=""
  bpa_mode="$(aws ec2 describe-vpc-block-public-access-options \
      --query 'VpcBlockPublicAccessOptions.InternetGatewayBlockMode' --output text 2>/dev/null || echo off)"
  if [[ -n "${NETWORK_MODE:-}" ]]; then
    network_mode="$NETWORK_MODE"
  elif [[ "$bpa_mode" == "block-ingress" || "$bpa_mode" == "block-bidirectional" ]]; then
    network_mode="private-nat"
    echo "VPC Block Public Access is '$bpa_mode' in this account/region: using a private subnet + NAT gateway (~0.045 USD/h extra)."
  else
    network_mode="public"
  fi

  if [[ "$network_mode" == "private-nat" ]]; then
    [[ -z "${ALLOWED_CIDR:-}" ]] || die "ALLOWED_CIDR cannot be used in private-nat mode (the instance has no public IP); access is through SSM only"
    az="$(aws ec2 describe-subnets --subnet-ids "$subnet_id" --query 'Subnets[0].AvailabilityZone' --output text)"
    private_cidr="${PRIVATE_SUBNET_CIDR:-$(free_private_cidr "$vpc_id")}"
    echo "Private subnet: $private_cidr in $az, NAT gateway in $subnet_id"
  fi

  if [[ -n "${ALLOWED_CIDR:-}" ]]; then
    echo "NOTE: port $HTTP_PORT will be reachable from $ALLOWED_CIDR on the public internet."
  else
    echo "Access: SSM port forwarding only (no inbound security-group rules)."
  fi
  echo "Deploying stack $STACK_NAME in $(aws configure get region 2>/dev/null || echo "${AWS_REGION:-${AWS_DEFAULT_REGION:-?}}") (vpc $vpc_id, subnet $subnet_id, mode $network_mode)"

  local params=(
    JwtSecretHex="$secret"
    UpstreamUrl="${UPSTREAM_URL:-https://postman-echo.com}"
    HttpPort="$HTTP_PORT"
    InstanceType="${INSTANCE_TYPE:-t4g.micro}"
    VpcId="$vpc_id"
    SubnetId="$subnet_id"
    NetworkMode="$network_mode"
  )
  [[ -n "${ALLOWED_CIDR:-}" ]] && params+=(AllowedCidr="$ALLOWED_CIDR")
  [[ "$network_mode" == "private-nat" ]] && params+=(AvailabilityZone="$az" PrivateSubnetCidr="$private_cidr")
  if [[ -n "$repo_url" ]]; then
    params+=(RepoUrl="$repo_url" GitRef="$git_ref")
  else
    params+=(SourceBucket="$source_bucket" SourceKey="$source_key")
  fi

  # --disable-rollback keeps a failed instance around so it can be inspected over SSM.
  aws cloudformation deploy \
    --stack-name "$STACK_NAME" \
    --template-file "$here/template.yaml" \
    --capabilities CAPABILITY_IAM \
    --disable-rollback \
    --no-fail-on-empty-changeset \
    --parameter-overrides "${params[@]}"

  cmd_status
}

cmd_status() {
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
      --query 'Stacks[0].{Status:StackStatus,Outputs:Outputs[].{Key:OutputKey,Value:OutputValue}}' --output table
}

check_plugin() {
  command -v session-manager-plugin >/dev/null 2>&1 || die "the Session Manager plugin is required:
  macOS:  brew install --cask session-manager-plugin
  other:  https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html"
}

cmd_tunnel() {
  check_plugin
  local id; id="$(instance_id)"
  echo "Forwarding http://localhost:8080 -> $id:$HTTP_PORT  (Ctrl+C to stop)"
  echo "Try: curl -s http://localhost:8080/status"
  aws ssm start-session --target "$id" --document-name AWS-StartPortForwardingSession \
      --parameters "{\"portNumber\":[\"$HTTP_PORT\"],\"localPortNumber\":[\"8080\"]}"
}

cmd_shell() {
  check_plugin
  aws ssm start-session --target "$(instance_id)"
}

cmd_update() {
  local id cmd_id repo_url fetch region source_bucket source_key
  id="$(instance_id)"
  repo_url="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
      --query "Stacks[0].Parameters[?ParameterKey=='RepoUrl'].ParameterValue" --output text)"
  if [[ -n "$repo_url" && "$repo_url" != "None" ]]; then
    echo "Updating $id: git pull + docker compose up --build"
    fetch='"git pull --ff-only"'
  else
    echo "Updating $id: uploading HEAD to S3, then extract + docker compose up --build"
    read -r source_bucket source_key <<<"$(upload_source | tail -n 1)"
    region="$(aws configure get region 2>/dev/null || echo "${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}")"
    fetch="\"aws s3 cp s3://$source_bucket/$source_key /tmp/src.tgz --region $region\",\"tar -xzf /tmp/src.tgz -C /opt/jwt-proxy\",\"rm -f /tmp/src.tgz\""
  fi
  cmd_id="$(aws ssm send-command --instance-ids "$id" --document-name AWS-RunShellScript \
      --comment "jwt-proxy update" \
      --parameters "commands=[\"set -e\",\"cd /opt/jwt-proxy\",$fetch,\"docker compose up -d --build proxy\",\"sleep 3\",\"curl -fsS http://127.0.0.1:$HTTP_PORT/status\"]" \
      --query 'Command.CommandId' --output text)"
  aws ssm wait command-executed --command-id "$cmd_id" --instance-id "$id" || true
  aws ssm get-command-invocation --command-id "$cmd_id" --instance-id "$id" \
      --query '{Status:Status,Output:StandardOutputContent,Error:StandardErrorContent}' --output table
}

cmd_down() {
  echo "Deleting stack $STACK_NAME (instance, security group, role)..."
  aws cloudformation delete-stack --stack-name "$STACK_NAME"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME"
  echo "done"
}

action="${1:-up}"
case "$action" in
  up|status|tunnel|shell|update|down) "cmd_$action" ;;
  *) sed -n '2,25p' "$0"; exit 1 ;;
esac
