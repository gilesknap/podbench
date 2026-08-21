#!/usr/bin/env bash
#
# Provision a namespace-confined ServiceAccount for Claude, and prove it is
# confined.
#
#   ./k8s/make-claude-sa.sh <namespace> [--podbench[=TIERS]] [--duration 24h]
#
# Creates `claude-$USER` in <namespace>, a Role and RoleBinding beside it, then
# writes a self-contained kubeconfig to k8s/<namespace>-claude-<user>.kubeconfig
# (gitignored) and runs the confinement checks against it.
#
# --podbench grants the verbs podbench needs, in the same tiers the chart uses:
#   observe  attach: ephemeral container + exec           (the default tier)
#   iterate  `podbench dev`: create/delete pods, patch a Service selector
#   resize   `--resize`: get+patch pods/resize
#   hotfix   `podbench hotfix`: patch a pod template, which DEPLOYS CODE
# `--podbench` alone is observe; `--podbench=iterate,resize` adds to it;
# `--podbench-all` is every tier. Nothing here can run the e2e suite: that
# creates and deletes its own namespaces and binds cluster-scoped admission
# policies, neither of which a namespace-confined account can do.
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
TIER_OBSERVE=0
TIER_ITERATE=0
TIER_RESIZE=0
TIER_HOTFIX=0

usage() {
  sed -n '3,20p' "$0" | sed 's/^#\s\?//'
  exit "${1:-1}"
}

# Every tier implies observe: iterate, resize and hotfix all end in an attach,
# and an attach without pods/exec is a seat nothing can reach.
add_tiers() { # add_tiers <comma-separated tier list>
  local tier
  local IFS=,
  for tier in $1; do
    case "$tier" in
      all)     TIER_OBSERVE=1; TIER_ITERATE=1; TIER_RESIZE=1; TIER_HOTFIX=1 ;;
      observe) TIER_OBSERVE=1 ;;
      iterate) TIER_OBSERVE=1; TIER_ITERATE=1 ;;
      resize)  TIER_OBSERVE=1; TIER_RESIZE=1 ;;
      hotfix)  TIER_OBSERVE=1; TIER_HOTFIX=1 ;;
      *) echo "unknown podbench tier '$tier'" >&2
         echo "want one or more of: observe,iterate,resize,hotfix,all" >&2
         exit 1 ;;
    esac
  done
  PODBENCH=1
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)     usage 0 ;;
    --podbench)    add_tiers observe; shift ;;
    --podbench=*)  add_tiers "${1#*=}"; shift ;;
    --podbench-all) add_tiers all; shift ;;
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

TIERS=""
for pair in "observe:$TIER_OBSERVE" "iterate:$TIER_ITERATE" \
            "resize:$TIER_RESIZE" "hotfix:$TIER_HOTFIX"; do
  [ "${pair#*:}" = 1 ] && TIERS="${TIERS}${TIERS:+,}${pair%%:*}"
done

echo "==> provisioning $SA in $NS"
echo "    podbench tiers: ${TIERS:-none (read-only)}"

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
                "persistentvolumeclaims", "events", "endpoints",
                "limitranges"]
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

# The tiers below mirror Charts/podbench/templates/rbac.yaml, whose comments
# say why each verb exists. Keep the two in step: podbench's own `doctor` verb
# reports a missing grant by naming the chart flag that gives it, so a tier that
# is generous here and absent there sends a reader somewhere that cannot help.
if [ "$TIER_OBSERVE" = 1 ]; then
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

if [ "$TIER_ITERATE" = 1 ]; then
  # Iterate mode mints a sacrificial dev pod from the target'"'"'s spec and deletes
  # it on teardown. The Service patch is `--take-traffic`/`--cutover` only, and
  # is also how someone accidentally takes production traffic.
  RULES="${RULES}"'
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["create", "delete"]
  - apiGroups: [""]
    resources: ["services"]
    verbs: ["patch"]'
fi

if [ "$TIER_RESIZE" = 1 ]; then
  # A seat shares the workload'"'"'s memory limit and cannot reserve its own, so an
  # editor session can get the workload OOM-killed. In-place resize is the
  # mitigation, and it changes a running workload'"'"'s limits.
  #
  # `get` as well as `patch`. kubectl reads the subresource back before it
  # writes it, so `patch` alone gets a Forbidden on that GET and no PATCH is
  # ever sent - measured with `--v=8` on hgv27681, 2026-08-20. Both Diamond
  # clusters were provisioned by this script while it granted `patch` alone,
  # and `get pods/resize` is Forbidden on both: the tier they hold cannot
  # actuate a resize at all.
  RULES="${RULES}"'
  - apiGroups: [""]
    resources: ["pods/resize"]
    verbs: ["get", "patch"]'
fi

if [ "$TIER_HOTFIX" = 1 ]; then
  # The provenance annotations go on the workload'"'"'s pod template, because pod
  # annotations do not survive the reschedule hotfix mode relies on. That write
  # is therefore also what rolls the workload - the mechanism `kubectl rollout
  # restart` uses - so this verb DEPLOYS CODE, and it is the most privileged
  # thing on this list. An unowned pod takes the annotations directly and is
  # never deleted; a controlled one is deleted so its controller replaces it.
  RULES="${RULES}"'
  - apiGroups: ["apps"]
    resources: ["deployments", "statefulsets"]
    verbs: ["patch"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["patch", "delete"]'
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

# What a tier flag says the answer should be. Hard-coding "no" for a verb the
# caller explicitly asked for would fail the script for doing its job, and
# hard-coding "yes" would stop these checks noticing a Role that did not apply.
tier() { if [ "$1" = 1 ]; then echo yes; else echo no; fi; }

# Both iterate (teardown of its dev pod) and hotfix (replacing a controlled pod)
# need it, so it is granted if either is.
DELETE_PODS=0
if [ "$TIER_ITERATE" = 1 ] || [ "$TIER_HOTFIX" = 1 ]; then DELETE_PODS=1; fi

echo
echo "==> confinement checks (as ${SA})"
echo "    inside ${NS}:"
expect yes "read pods"                       get pods -n "$NS"
expect yes "read deployments"                get deployments.apps -n "$NS"
expect yes "read limitranges"                get limitranges -n "$NS"
# Never granted, at any tier: with secrets the SA could read every other
# ServiceAccount token in the namespace and become them.
expect no  "read secrets"                    get secrets -n "$NS"
# --subresource, never `pods/exec` as one word. kubectl splits that argument on
# the slash into resource and resource *name*, so `can-i create pods/exec` asks
# whether you may create a pod called "exec" - a different question, answered
# "no" by a Role that grants the exec subresource perfectly well. It reads as
# correct because a cluster-admin gets "yes" to both readings; only a
# narrowly-scoped account like this one can tell them apart.
#
# The same misreading also answers "yes" to a permission nobody has, which is
# the direction that hides a broken resize tier: `can-i get pods/resize` is
# "may I read a pod named resize", so it inherits `get pods` and says yes.
# Measured on the k3s bed 2026-08-21, against an account holding
# `pods/resize: [patch]`: `can-i get pods/resize` yes, `can-i get pods
# --subresource=resize` no, and a real resize Forbidden on the GET. Only the
# --subresource form asks the API server the question kubectl will ask.
expect "$(tier "$TIER_OBSERVE")" "exec into pods" \
                                             create pods --subresource=exec -n "$NS"
expect "$(tier "$TIER_OBSERVE")" "add ephemeral container" \
                                             update pods --subresource=ephemeralcontainers -n "$NS"
expect "$(tier "$TIER_ITERATE")" "create pods (iterate)" \
                                             create pods -n "$NS"
expect "$(tier "$TIER_ITERATE")" "patch services (iterate)" \
                                             patch services -n "$NS"
expect "$(tier "$DELETE_PODS")"  "delete pods" \
                                             delete pods -n "$NS"
expect "$(tier "$TIER_RESIZE")"  "write pods/resize (resize in place)" \
                                             patch pods --subresource=resize -n "$NS"
# Checked separately from the patch because it fails separately: kubectl GETs
# the subresource before it writes it, so an account holding `patch` alone gets
# as far as the read and stops. This check is what the tier is worth.
expect "$(tier "$TIER_RESIZE")"  "read pods/resize (kubectl GETs it first)" \
                                             get pods --subresource=resize -n "$NS"
expect "$(tier "$TIER_HOTFIX")"  "patch pods (hotfix)" \
                                             patch pods -n "$NS"
expect "$(tier "$TIER_HOTFIX")"  "patch deployments (hotfix: deploys code)" \
                                             patch deployments.apps -n "$NS"

echo "    outside ${NS} (control: ${CONTROL}):"
expect no "read pods in ${CONTROL}"          get pods -n "$CONTROL"
expect no "read secrets in ${CONTROL}"       get secrets -n "$CONTROL"
expect no "exec into pods in ${CONTROL}"     create pods --subresource=exec -n "$CONTROL"
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

# The resize tier, exercised rather than asked about. Everything above is a
# SelfSubjectAccessReview; this is the actual first request `kubectl patch
# --subresource=resize` makes, against a real pod, and it is the request that
# is Forbidden on both Diamond clusters while the tier's own checks say yes.
# Reading a subresource mutates nothing, so it is safe on a live namespace -
# and there is no honest way to exercise the write half without resizing
# somebody's workload, which this script will not do.
if [ "$TIER_RESIZE" = 1 ]; then
  VICTIM=$($K get pods -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  printf '  '
  if [ -z "$VICTIM" ]; then
    printf '[info] no pod in %s to exercise pods/resize against\n' "$NS"
  elif ERR=$($K get "pod/${VICTIM}" --subresource=resize 2>&1 >/dev/null); then
    printf '[ok]   live read of pods/resize on %s\n' "$VICTIM"
  elif printf '%s' "$ERR" | grep -qi forbidden; then
    printf '[FAIL] live read of pods/resize on %s: %s\n' "$VICTIM" "$ERR"; FAILED=1
  else
    # An older kubectl rejects --subresource=resize itself, and a pod that went
    # away between the two calls is not a permission problem either. Neither is
    # evidence about the grant, so neither fails the script.
    printf '[info] pods/resize not exercised: %s\n' "$ERR"
  fi
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
  if [ "$PODBENCH" = 1 ]; then
    echo "    check it with:  KUBECONFIG=${OUT} uvx podbench doctor -n ${NS}"
    # The e2e suite is not a thing this credential can run, and finding that out
    # from a wall of namespace-creation failures costs an afternoon.
    echo "    note: tests/e2e creates its own namespaces and binds cluster-scoped"
    echo "          admission policies, so it needs a credential this is not."
  fi
else
  echo "==> FAIL: confinement is not what was asked for. Do not hand this over." >&2
  exit 1
fi
