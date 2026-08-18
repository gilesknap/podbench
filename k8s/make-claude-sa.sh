#!/usr/bin/env bash
#
# Provision a namespace-confined ServiceAccount for Claude, and prove it is
# confined.
#
#   ./k8s/make-claude-sa.sh <namespace> [--podbench] [--duration 24h]
#
# Creates `claude-$USER` in <namespace>, a Role and RoleBinding beside it, then
# writes a self-contained kubeconfig to k8s/<namespace>-claude-<user>.kubeconfig
# (gitignored) and runs the confinement checks against it.
#
# Run it with YOUR OWN admin credential: it reads your current context for the
# API server address and CA, and needs create rights on serviceaccounts, roles
# and rolebindings in <namespace>.
#
# Re-running is safe: every object is applied, not created, so this is also how
# you refresh an expired token.
set -euo pipefail

# --- arguments -------------------------------------------------------------
NS=""
DURATION=24h
PODBENCH=0

usage() {
  sed -n '3,12p' "$0" | sed 's/^#\s\?//'
  exit "${1:-1}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)     usage 0 ;;
    --podbench)    PODBENCH=1; shift ;;
    --duration)    DURATION=${2:?--duration needs a value}; shift 2 ;;
    --duration=*)  DURATION=${1#*=}; shift ;;
    -*)            echo "unknown option: $1" >&2; usage ;;
    *)             [ -z "$NS" ] || { echo "one namespace only" >&2; usage; }
                   NS=$1; shift ;;
  esac
done
[ -n "$NS" ] || usage

command -v kubectl >/dev/null || { echo "kubectl not on PATH" >&2; exit 1; }

# --- who is this for? ------------------------------------------------------
# $USER is frequently unset in a devcontainer or a CI shell, and an SA called
# "claude-" is both invalid and useless for telling two people apart.
WHO=${USER:-$(id -un 2>/dev/null || echo unknown)}
# A ServiceAccount name is an RFC 1123 label: lowercase alphanumeric and '-',
# starting and ending alphanumeric. Diamond's fedids are clean, but a name with
# a dot or an upper-case letter is rejected by the API server with a message
# that does not mention which field, so normalise rather than find out.
WHO=$(printf '%s' "$WHO" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' \
      | sed -e 's/^-*//' -e 's/-*$//')
[ -n "$WHO" ] || WHO=unknown
SA="claude-${WHO}"

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
OUT="${HERE}/${NS}-${SA}.kubeconfig"

kubectl get namespace "$NS" >/dev/null 2>&1 \
  || { echo "namespace '$NS' does not exist (or you cannot see it)" >&2; exit 1; }

echo "==> provisioning $SA in $NS"

# --- the RBAC --------------------------------------------------------------
# Role and RoleBinding, never ClusterRole/ClusterRoleBinding: a namespaced
# binding is the entire confinement mechanism. Nothing granted here exists in
# any other namespace.
#
# `secrets` is deliberately absent. With it, the SA could read every other
# ServiceAccount's token in this namespace and become them, which would undo the
# binding above from inside.
RULES='
  - apiGroups: [""]
    resources: ["pods", "pods/log", "services", "configmaps",
                "persistentvolumeclaims", "events", "endpoints"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["deployments", "statefulsets", "replicasets", "daemonsets"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["batch"]
    resources: ["jobs", "cronjobs"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["metrics.k8s.io"]
    resources: ["pods"]
    verbs: ["get", "list"]'

if [ "$PODBENCH" = 1 ]; then
  # podbench's `observe` tier, matching Charts/podbench/templates/rbac.yaml.
  # `update` on the subresource is the one that matters: the seat is added by
  # PUTting pods/ephemeralcontainers rather than shelling out to `kubectl
  # debug`, which merges its own profile over the spec and can hand back a rung
  # that is invalid by construction. pods/exec is the whole network story.
  RULES="${RULES}"'
  - apiGroups: [""]
    resources: ["pods/ephemeralcontainers"]
    verbs: ["get", "patch", "update"]
  - apiGroups: [""]
    resources: ["pods/exec"]
    verbs: ["create"]'
fi

kubectl apply -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${SA}
  namespace: ${NS}
  labels: {app.kubernetes.io/managed-by: podbench-k8s-script}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: ${SA}
  namespace: ${NS}
  labels: {app.kubernetes.io/managed-by: podbench-k8s-script}
rules:${RULES}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: ${SA}
  namespace: ${NS}
  labels: {app.kubernetes.io/managed-by: podbench-k8s-script}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: ${SA}
subjects:
  - kind: ServiceAccount
    name: ${SA}
    namespace: ${NS}
EOF

# --- the kubeconfig --------------------------------------------------------
CTX=$(kubectl config current-context)
CLUSTER=$(kubectl config view -o jsonpath="{.contexts[?(@.name==\"$CTX\")].context.cluster}")
[ -n "$CLUSTER" ] || { echo "could not resolve cluster for context $CTX" >&2; exit 1; }

field() { kubectl config view --raw -o jsonpath="{.clusters[?(@.name==\"$CLUSTER\")].cluster.$1}"; }
SERVER=$(field server)
CA_DATA=$(field certificate-authority-data)
CA_FILE=$(field certificate-authority)
INSECURE=$(field insecure-skip-tls-verify)

# The CA is embedded in some kubeconfigs and a filesystem path in others
# (kubeadm, k3s, minikube). Normalise to embedded, so the file we hand over does
# not reference a path that exists only on this machine.
if [ -z "$CA_DATA" ] && [ -n "$CA_FILE" ]; then
  CA_DATA=$(base64 < "$CA_FILE" | tr -d '\n')
fi
if [ -n "$CA_DATA" ]; then
  TLS="certificate-authority-data: ${CA_DATA}"
elif [ "$INSECURE" = "true" ]; then
  echo "WARNING: cluster is configured insecure-skip-tls-verify; carrying that over" >&2
  TLS="insecure-skip-tls-verify: true"
else
  # Neither field is legitimate: the server presents a certificate the system
  # trust store already covers. Emit nothing rather than invent a CA.
  TLS=""
fi

TOKEN=$(kubectl -n "$NS" create token "$SA" --duration="$DURATION")

umask 077
cat > "$OUT" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: target
    cluster:
      server: ${SERVER}
      ${TLS}
users:
  - name: ${SA}
    user:
      token: ${TOKEN}
contexts:
  - name: ${SA}
    context:
      cluster: target
      user: ${SA}
      namespace: ${NS}
current-context: ${SA}
EOF
chmod 600 "$OUT"
echo "==> wrote ${OUT}"

# The API server silently clamps --duration to its own
# --service-account-max-token-expiration, so report the token's own claim rather
# than the flag we asked for.
EXP=$(printf '%s' "$TOKEN" | cut -d. -f2 | tr '_-' '/+' | sed 's/$/==/' \
      | base64 -d 2>/dev/null \
      | python3 -c 'import sys,json,datetime;print(datetime.datetime.fromtimestamp(json.load(sys.stdin)["exp"]))' \
      2>/dev/null || true)
if [ -n "$EXP" ]; then
  echo "==> token expires ${EXP} (asked for ${DURATION})"
else
  echo "==> token expiry could not be decoded (needs base64 -d and python3)"
fi

# --- verification ----------------------------------------------------------
# Everything below runs as the NEW credential, never as yours.
K="kubectl --kubeconfig ${OUT}"

# A control namespace to prove denial against. It has to be one we did not just
# grant, and `default` exists on every cluster.
CONTROL=default
[ "$NS" != "$CONTROL" ] || CONTROL=kube-system

# `kubectl auth can-i` exits 1 for a legitimate "no", which under `set -e` would
# kill the script at the first passing check. Capture, never let it propagate.
can_i() { $K auth can-i "$@" 2>/dev/null || true; }

FAILED=0
expect() { # expect <yes|no> <label> <can-i args...>
  local want=$1 label=$2; shift 2
  local got; got=$(can_i "$@")
  if [ "$got" = "$want" ]; then
    printf '  [ok]   %-46s %s\n' "$label" "$got"
  else
    printf '  [FAIL] %-46s %s (wanted %s)\n' "$label" "${got:-error}" "$want"
    FAILED=1
  fi
}

echo
echo "==> confinement checks (as ${SA})"
echo "    inside ${NS}:"
expect yes "read pods"                       get pods -n "$NS"
expect yes "read deployments"                get deployments.apps -n "$NS"
expect no  "read secrets"                    get secrets -n "$NS"
expect no  "create pods"                     create pods -n "$NS"
expect no  "delete pods"                     delete pods -n "$NS"
expect no  "patch deployments"               patch deployments.apps -n "$NS"
if [ "$PODBENCH" = 1 ]; then
  expect yes "exec into pods (--podbench)"     create pods/exec -n "$NS"
  expect yes "add ephemeral container"         update pods/ephemeralcontainers -n "$NS"
else
  expect no  "exec into pods"                  create pods/exec -n "$NS"
fi

echo "    outside ${NS} (control: ${CONTROL}):"
expect no "read pods in ${CONTROL}"          get pods -n "$CONTROL"
expect no "read secrets in ${CONTROL}"       get secrets -n "$CONTROL"
expect no "exec into pods in ${CONTROL}"     create pods/exec -n "$CONTROL"
expect no "anything, anywhere"               '*' '*' --all-namespaces

# auth can-i is a SelfSubjectAccessReview answered by the API server's
# authorizers. On a cluster with a webhook authorizer that and the real request
# can in principle disagree, so make one actual cross-namespace read and require
# it to be refused.
printf '  '
if $K get pods -n "$CONTROL" >/dev/null 2>&1; then
  printf '[FAIL] live read of %s/pods succeeded\n' "$CONTROL"; FAILED=1
else
  printf '[ok]   live read of %s/pods refused\n' "$CONTROL"
fi
printf '  '
if $K get pods --all-namespaces >/dev/null 2>&1; then
  printf '[FAIL] live cluster-wide pod list succeeded\n'; FAILED=1
else
  printf '[ok]   live cluster-wide pod list refused\n'
fi

# --- inherited cluster-scoped reads ----------------------------------------
# Anything the SA can read cluster-wide that this script did not grant comes
# from a pre-existing ClusterRoleBinding, usually onto system:authenticated.
# Deleting our RoleBinding would not remove it, and no Role can subtract it, so
# these are REPORTED and never failed: treating a site-wide policy as a defect
# in this script would make the check cry wolf on every run.
echo
echo "==> cluster-scoped reads inherited from cluster policy (not granted here)"
INHERITED=0
report() { # report <label> <can-i args...>
  local label=$1; shift
  if [ "$(can_i "$@")" = yes ]; then
    printf '  [info] %s\n' "$label"; INHERITED=1
  fi
}
report "can list namespaces"              list namespaces
report "can read nodes"                   get nodes
report "can list persistentvolumes"       list persistentvolumes
report "can list customresourcedefinitions" list customresourcedefinitions.apiextensions.k8s.io
report "can list storageclasses"          list storageclasses.storage.k8s.io
if [ "$INHERITED" = 1 ]; then
  cat <<'EOM'
  These expose cluster inventory - node names, namespace names, volume
  claimRefs - but no workload data outside the namespace above. To find what
  grants them, run with your own credential:
    kubectl get clusterrolebindings -o json | jq -r '.items[]
      | select(.subjects[]? | .name=="system:authenticated"
                          or .name=="system:serviceaccounts")
      | .metadata.name + "  ->  " + .roleRef.name'
EOM
else
  echo "  (none)"
fi

echo
if [ "$FAILED" = 0 ]; then
  echo "==> PASS: ${SA} reaches ${NS} and nothing else."
  echo "    use it with:  KUBECONFIG=${OUT}"
else
  echo "==> FAIL: confinement is not what was asked for. Do not hand this over." >&2
  exit 1
fi
