#!/usr/bin/env bash
#
# Reach a cluster's API through an ssh tunnel, and write the kubeconfig for it.
#
#   ./k8s/vpn-api-tunnel.sh [user@host:]<kubeconfig> [--ssh-host <host>]
#   ./k8s/vpn-api-tunnel.sh [user@host:]<kubeconfig> --stop
#
# For working from home over a VPN that forwards ssh and nothing else. podbench
# needs no port-forward, no pod IP and no Service - the seat is reached through
# `kubectl exec` - so a reachable API server is the whole requirement, and one
# tunnel supplies it.
#
# Takes an existing kubeconfig (one k8s/make-claude-sa.sh wrote, or your own),
# forwards the API server's address to a local port through <host>, and writes a
# copy of the kubeconfig pointing at that port. Everything else in it - the
# token, the CA, the namespace - is carried over untouched.
#
# The kubeconfig may be named scp-style as `[user@]host:path`, in which case it
#   is read over ssh and never lands on this machine: it is only ever input to
#   the copy this script writes, and it holds a live bearer token. That host is
#   also the default --ssh-host, since a machine holding the kubeconfig is
#   usually a machine that can reach the API server it names.
# --ssh-host is any machine your ssh reaches that can itself reach the API
#   server. The tunnel exits from there, so that is the address the API server
#   sees, which matters where access is allow-listed by source IP.
# --local-port defaults to 6443, and to the next free port above it if that is
#   taken by something else. A tunnel this script started is reused, not
#   duplicated.
# --config-only writes the kubeconfig and prints the ssh command without running
#   it, for a tunnel you manage yourself (autossh, a systemd unit, an existing
#   session).
# --out names the kubeconfig to write. It defaults to the source's name with
#   `-tunnel` before the extension, beside the source - or beside this script,
#   where the source is remote and has no local directory to sit beside.
# --stop closes the tunnel this script started for that kubeconfig.
#
# Re-running is safe: the kubeconfig is rewritten and the tunnel reused.
set -euo pipefail

# --- arguments -------------------------------------------------------------
SRC=""
SSH_HOST=""
LOCAL_PORT=""
OUT=""
CONFIG_ONLY=0
STOP=0

usage() {
  sed -n '3,37p' "$0" | sed 's/^#\s\?//'
  exit "${1:-1}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)       usage 0 ;;
    --ssh-host)      SSH_HOST=${2:?--ssh-host needs a value}; shift 2 ;;
    --ssh-host=*)    SSH_HOST=${1#*=}; shift ;;
    --local-port)    LOCAL_PORT=${2:?--local-port needs a value}; shift 2 ;;
    --local-port=*)  LOCAL_PORT=${1#*=}; shift ;;
    --out)           OUT=${2:?--out needs a value}; shift 2 ;;
    --out=*)         OUT=${1#*=}; shift ;;
    --config-only)   CONFIG_ONLY=1; shift ;;
    --stop)          STOP=1; shift ;;
    -*)              echo "unknown option: $1" >&2; usage ;;
    *)               [ -z "$SRC" ] || { echo "one kubeconfig only" >&2; usage; }
                     SRC=$1; shift ;;
  esac
done
[ -n "$SRC" ] || usage
command -v kubectl >/dev/null || { echo "kubectl not on PATH" >&2; exit 1; }
command -v ssh >/dev/null || { echo "ssh not on PATH" >&2; exit 1; }

# --- local or remote source ------------------------------------------------
# scp's own rule: a colon before the first slash names a host. It is what makes
# `./k8s/a:b.kubeconfig` local and `box:k8s/a.kubeconfig` remote, without a flag
# to say which.
SRC_SPEC=$SRC
SRC_HOST=""
case "$SRC" in
  *:*)
    # Everything up to the first colon. A slash in there means the colon is
    # part of a path (`./k8s/a:b.kubeconfig`) and not a host separator.
    case "${SRC%%:*}" in
      ""|*/*) ;;
      *) SRC_HOST=${SRC%%:*} ;;
    esac
    ;;
esac
# SRC_NAME is the path without any host, and is what the output is named
# after. Stripping to the first colon unconditionally would rename a local
# `tmp/od:d.kubeconfig` to `d-tunnel.kubeconfig`.
if [ -n "$SRC_HOST" ]; then
  SRC_PATH=${SRC#*:}
  [ -n "$SRC_PATH" ] || { echo "no path after '${SRC_HOST}:'" >&2; exit 1; }
  SRC_NAME=$SRC_PATH
else
  [ -r "$SRC" ] || { echo "cannot read kubeconfig: ${SRC}" >&2; exit 1; }
  SRC_NAME=$SRC
fi

# --- the tunnel's identity -------------------------------------------------
# Derived from the source kubeconfig so that --stop finds the tunnel started for
# it, and so that two clusters get two tunnels rather than one that moves. Keyed
# on what was typed for a remote source, because the local copy of that is a
# temporary file with a different name on every run. The socket lives under
# $HOME and is kept short: a unix socket path is capped at ~104 bytes and ssh
# fails obscurely past it.
if [ -n "$SRC_HOST" ]; then
  SRC_KEY=$SRC_SPEC
else
  SRC_KEY=$(cd "$(dirname "$SRC")" && pwd)/$(basename "$SRC")
fi
TAG=$(basename "$SRC_NAME" | sed 's/\.kubeconfig$//; s/[^A-Za-z0-9._-]/_/g')
SOCK_DIR="${HOME}/.podbench"
SOCK="${SOCK_DIR}/tun-$(printf '%s' "$SRC_KEY" | cksum | cut -d' ' -f1).sock"

tunnel_up() { ssh -S "$SOCK" -O check placeholder >/dev/null 2>&1; }

if [ "$STOP" = 1 ]; then
  if tunnel_up; then
    ssh -S "$SOCK" -O exit placeholder >/dev/null 2>&1 || true
    echo "==> closed the tunnel for ${TAG}"
  else
    echo "==> no tunnel running for ${TAG}"
  fi
  exit 0
fi

# --- fetch a remote kubeconfig ---------------------------------------------
# Read over ssh into a 0600 temporary file this script removes on any exit,
# rather than copied here to stay.
# The point of naming it remotely is that it does not have to live here: it is
# input to the copy written below and nothing else, and it carries a live
# bearer token for a real cluster.
#
# `cat` rather than scp: it needs no sftp subsystem at the far end, and the
# error for a missing file arrives on stderr in the far end's own words.
if [ -n "$SRC_HOST" ]; then
  umask 077
  SRC=$(mktemp "${TMPDIR:-/tmp}/podbench-kubeconfig.XXXXXX")
  trap 'rm -f "$SRC"' EXIT INT TERM
  if ! ssh "$SRC_HOST" "cat -- $(printf '%q' "$SRC_PATH")" > "$SRC"; then
    echo "could not read ${SRC_SPEC}" >&2
    exit 1
  fi
  [ -s "$SRC" ] || { echo "${SRC_SPEC} is empty" >&2; exit 1; }
  echo "==> read ${SRC_SPEC} over ssh (not kept here)"
  # The machine that holds a cluster's kubeconfig is usually one that can reach
  # the API server it names, so it is the tunnel's default exit. Stated, never
  # assumed silently: it decides which address the API server sees.
  if [ -z "$SSH_HOST" ]; then
    SSH_HOST=$SRC_HOST
    echo "    --ssh-host defaults to ${SSH_HOST}, the host it came from"
  fi
fi

# --- read the source kubeconfig --------------------------------------------
# --minify reduces it to the current context, so `[0]` is that context's cluster
# and not whichever happens to be listed first.
kc() { kubectl --kubeconfig "$SRC" config view --minify -o "jsonpath=$1"; }

SERVER=$(kc '{.clusters[0].cluster.server}')
CLUSTER=$(kc '{.clusters[0].name}')
NS=$(kc '{.contexts[0].context.namespace}')
# Bracket form, not dotted: the field name contains dashes.
INSECURE=$(kc "{.clusters[0].cluster['insecure-skip-tls-verify']}")
PROXY=$(kc "{.clusters[0].cluster['proxy-url']}")
CONTEXT=$(kubectl --kubeconfig "$SRC" config current-context)

[ -n "$SERVER" ] || { echo "no server address in ${SRC_SPEC}'s current context" >&2; exit 1; }
case "$SERVER" in
  https://*) ;;
  *) echo "server is ${SERVER}, and only https is tunnelled here" >&2; exit 1 ;;
esac

HOSTPORT=${SERVER#https://}
HOSTPORT=${HOSTPORT%%/*}
case "$HOSTPORT" in
  \[*\]:*) API_HOST=${HOSTPORT%]:*}; API_HOST=${API_HOST#[}; API_PORT=${HOSTPORT##*]:} ;;
  \[*\])   API_HOST=${HOSTPORT#[}; API_HOST=${API_HOST%]}; API_PORT=443 ;;
  *:*)     API_HOST=${HOSTPORT%:*}; API_PORT=${HOSTPORT##*:} ;;
  *)       API_HOST=$HOSTPORT; API_PORT=443 ;;
esac

if [ "$CONFIG_ONLY" = 0 ] && [ -z "$SSH_HOST" ]; then
  echo "--ssh-host is required: name a machine your ssh reaches that can" >&2
  echo "itself reach ${API_HOST}:${API_PORT}" >&2
  exit 1
fi

# --- choose the local port -------------------------------------------------
# A port our own tunnel already holds is the answer, not a collision: re-running
# this script must not leave two tunnels and two kubeconfigs disagreeing.
port_free() { ! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

if [ -z "$LOCAL_PORT" ]; then
  LOCAL_PORT=6443
  if ! port_free "$LOCAL_PORT" && ! tunnel_up; then
    while [ "$LOCAL_PORT" -lt 6500 ] && ! port_free "$LOCAL_PORT"; do
      LOCAL_PORT=$((LOCAL_PORT + 1))
    done
    [ "$LOCAL_PORT" -lt 6500 ] || { echo "no free port in 6443-6499" >&2; exit 1; }
  fi
fi

# --- how to use it ---------------------------------------------------------
# A function rather than a trailing block, because --config-only needs it too:
# somebody running their own forward has the same kubeconfig and the same
# ProxyCommand caveat, and printing it on one path only is how half the
# instructions go missing.
usage_notes() {
cat <<EOM

==> use it with:
      export KUBECONFIG=${OUT}
      uvx podbench doctor${NS:+ -n ${NS}}
      uvx podbench vscode <pod>${NS:+ -n ${NS}}

    Run podbench from a terminal on THIS machine, not over ssh on a cluster-side
    box: \`podbench vscode\` refuses a \`code\` that resolves to VS Code's remote
    CLI, because --install-extension there installs into the wrong machine.

    One caveat, and it decides whether a reconnect works. The ssh stanza podbench
    writes carries a ProxyCommand that runs \`kubectl exec\`, and it does not
    embed --kubeconfig - it inherits the environment of whatever spawns it. The
    VS Code that podbench launches inherits the export above, so the first
    session is fine. A VS Code started later from a desktop icon has no
    KUBECONFIG and its ProxyCommand will look in ~/.kube/config instead. To make
    it survive that, merge this file in and give it a name of its own:

      KUBECONFIG=~/.kube/config:${OUT} kubectl config view --flatten > ~/.kube/config.new
      mv ~/.kube/config.new ~/.kube/config
      uvx podbench vscode <pod>${NS:+ -n ${NS}} --context ${CONTEXT}

    then the ProxyCommand's own --context resolves with no environment at all.

    close the tunnel with:
      $0 ${SRC_SPEC} --stop
EOM
}

# --- write the tunnelled kubeconfig ----------------------------------------
# Beside the source where there is one. A remote source has no local
# directory to sit beside, so it lands beside this script - which is `k8s/`,
# whose .gitignore already keeps `*.kubeconfig` out of the history.
if [ -z "$OUT" ]; then
  if [ -n "$SRC_HOST" ]; then
    OUT_DIR=$(cd "$(dirname "$0")" && pwd)
  else
    OUT_DIR=$(dirname "$SRC")
  fi
  OUT="${OUT_DIR}/$(basename "$SRC_NAME" .kubeconfig)-tunnel.kubeconfig"
fi

umask 077
kubectl --kubeconfig "$SRC" config view --raw --minify > "$OUT"

# The address changes and nothing else does. `tls-server-name` is what keeps the
# certificate valid: the server presents one for its own name, and pointing the
# client at 127.0.0.1 would otherwise fail verification for a name mismatch -
# the failure that tempts people into insecure-skip-tls-verify, which throws
# away the CA the source kubeconfig already carries.
if [ "$INSECURE" = "true" ]; then
  # `--insecure-skip-tls-verify=true` has to be restated, not merely left alone.
  # It is a boolean flag with a default, so `set-cluster --server` alone writes
  # the default over it and the copy silently becomes a config that verifies
  # against a CA it does not have. No `tls-server-name` beside it: the source
  # already declined to verify, and a server name there would suggest a check
  # that is not happening.
  kubectl --kubeconfig "$OUT" config set-cluster "$CLUSTER" \
    --server="https://127.0.0.1:${LOCAL_PORT}" \
    --insecure-skip-tls-verify=true >/dev/null
else
  kubectl --kubeconfig "$OUT" config set-cluster "$CLUSTER" \
    --server="https://127.0.0.1:${LOCAL_PORT}" \
    --tls-server-name="$API_HOST" >/dev/null
fi
chmod 600 "$OUT"

echo "==> ${SRC_SPEC} -> ${OUT}"
echo "    context   ${CONTEXT}${NS:+  (namespace ${NS})}"
echo "    API       ${API_HOST}:${API_PORT}  ->  127.0.0.1:${LOCAL_PORT}"
if [ "$INSECURE" = "true" ]; then
  echo "    TLS       not verified (carried over from ${SRC_SPEC})"
else
  echo "    TLS       verified as ${API_HOST}, through the source's own CA"
fi

# Carried over faithfully and almost certainly wrong now: client-go applies
# `proxy-url` to every connection the cluster entry makes, and the connection is
# now to 127.0.0.1. Said rather than silently removed, because a SOCKS entry
# somebody added on purpose is indistinguishable here from a corporate one that
# came with the file.
if [ -n "$PROXY" ]; then
  echo
  echo "  [warn] the source names proxy-url ${PROXY}, which is now applied to"
  echo "         127.0.0.1:${LOCAL_PORT} and will not work. Drop it with:"
  echo "           kubectl --kubeconfig ${OUT} config set-cluster ${CLUSTER} --proxy-url=''"
fi

SSH_ARGS="-fNT -M -S ${SOCK} -o ExitOnForwardFailure=yes"
SSH_FWD="-L 127.0.0.1:${LOCAL_PORT}:${API_HOST}:${API_PORT}"

if [ "$CONFIG_ONLY" = 1 ]; then
  echo
  echo "==> --config-only: start the forward yourself with"
  echo "    ssh -N ${SSH_FWD} ${SSH_HOST:-<host>}"
  usage_notes
  exit 0
fi

# --- the tunnel ------------------------------------------------------------
mkdir -p "$SOCK_DIR"
if tunnel_up; then
  echo "==> reusing the tunnel already running for ${TAG}"
else
  # ExitOnForwardFailure is what makes this honest: without it ssh backgrounds
  # happily when the forward was refused, and the first kubectl call fails with
  # a connection error that says nothing about ssh.
  # shellcheck disable=SC2086
  ssh $SSH_ARGS $SSH_FWD "$SSH_HOST"
  echo "==> tunnel up through ${SSH_HOST}"
fi

# --- verification ----------------------------------------------------------
# Through the new kubeconfig, never the old one: the point is that this file
# works, and a check that reaches the cluster some other way proves nothing.
echo
if VERSION=$(kubectl --kubeconfig "$OUT" --request-timeout=15s \
             version -o json 2>/dev/null \
             | sed -n 's/.*"gitVersion": *"\([^"]*\)".*/\1/p' | tail -1) \
   && [ -n "$VERSION" ]; then
  printf '  [ok]   %-42s %s\n' "API server reached through the tunnel" "$VERSION"
else
  printf '  [FAIL] %-42s\n' "API server not reachable through the tunnel"
  echo >&2
  echo "  ${SSH_HOST} may not reach ${API_HOST}:${API_PORT}, or the token in" >&2
  echo "  ${SRC_SPEC} may have expired. Ask the far end directly - bash's /dev/tcp" >&2
  echo "  needs nothing installed there, where nc and curl may be absent:" >&2
  echo "    ssh ${SSH_HOST} 'exec 3<>/dev/tcp/${API_HOST}/${API_PORT}' && echo reachable" >&2
  exit 1
fi

WHO=$(kubectl --kubeconfig "$OUT" --request-timeout=15s auth whoami \
      -o jsonpath='{.status.userInfo.username}' 2>/dev/null || true)
if [ -n "$WHO" ]; then
  printf '  [ok]   %-42s %s\n' "authenticated as" "$WHO"
else
  printf '  [--]   %-42s\n' "auth whoami unavailable (not fatal)"
fi

usage_notes
