#!/usr/bin/env bash
#
# Remove a Claude ServiceAccount and its RBAC from a namespace.
#
#   ./k8s/delete-claude-sa.sh <namespace> [--user <name>] [--all] [--yes]
#
# By default it removes claude-$USER. --user removes somebody else's, --all
# removes every account this repo's make-claude-sa.sh created in the namespace
# (matched by label, not by name pattern, so it cannot catch a bystander).
#
# There are exactly three cluster objects per account - ServiceAccount, Role,
# RoleBinding - plus any long-lived token Secret bound to the SA, and the local
# kubeconfig. Nothing else is created, so nothing else is cleaned up.
set -euo pipefail

LABEL=app.kubernetes.io/managed-by=podbench-k8s-script
NS=""
WHO=""
ALL=0
YES=0

usage() { sed -n '3,13p' "$0" | sed 's/^#\s\?//'; exit "${1:-1}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --user)    WHO=${2:?--user needs a value}; shift 2 ;;
    --user=*)  WHO=${1#*=}; shift ;;
    --all)     ALL=1; shift ;;
    -y|--yes)  YES=1; shift ;;
    -*)        echo "unknown option: $1" >&2; usage ;;
    *)         [ -z "$NS" ] || { echo "one namespace only" >&2; usage; }
               NS=$1; shift ;;
  esac
done
[ -n "$NS" ] || usage
command -v kubectl >/dev/null || { echo "kubectl not on PATH" >&2; exit 1; }

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# --- what are we deleting? -------------------------------------------------
if [ "$ALL" = 1 ]; then
  [ -z "$WHO" ] || { echo "--all and --user are mutually exclusive" >&2; exit 1; }
  # Match on the label make-claude-sa.sh stamps, so an account someone created
  # by hand and happened to call claude-something is left alone.
  mapfile -t NAMES < <(kubectl -n "$NS" get serviceaccounts -l "$LABEL" \
                        -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null || true)
else
  if [ -z "$WHO" ]; then
    WHO=${USER:-$(id -un 2>/dev/null || echo unknown)}
    WHO=$(printf '%s' "$WHO" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' \
          | sed -e 's/^-*//' -e 's/-*$//')
  fi
  NAMES=("claude-${WHO#claude-}")
fi

if [ "${#NAMES[@]}" -eq 0 ] || [ -z "${NAMES[0]}" ]; then
  echo "nothing to delete in ${NS}"; exit 0
fi

echo "about to delete from namespace '${NS}':"
for n in "${NAMES[@]}"; do
  printf '  serviceaccount/%s  role/%s  rolebinding/%s\n' "$n" "$n" "$n"
done

if [ "$YES" != 1 ]; then
  # This deletes objects in a live namespace, so it asks unless told not to.
  read -r -p "proceed? [y/N] " reply
  case "$reply" in [yY]*) ;; *) echo "aborted"; exit 1 ;; esac
fi

# --- delete ----------------------------------------------------------------
for n in "${NAMES[@]}"; do
  # --ignore-not-found so a partial previous teardown, or an account whose Role
  # was never created, still finishes rather than aborting under `set -e`.
  kubectl -n "$NS" delete --ignore-not-found \
    "serviceaccount/${n}" "role.rbac.authorization.k8s.io/${n}" \
    "rolebinding.rbac.authorization.k8s.io/${n}"

  # A long-lived token Secret is only present if someone chose that route over
  # `kubectl create token`; it is bound to the SA by annotation, not by name.
  mapfile -t SECRETS < <(kubectl -n "$NS" get secrets \
    --field-selector type=kubernetes.io/service-account-token \
    -o jsonpath="{range .items[?(@.metadata.annotations['kubernetes\.io/service-account\.name']=='${n}')]}{.metadata.name}{'\n'}{end}" \
    2>/dev/null || true)
  for s in "${SECRETS[@]}"; do
    [ -n "$s" ] || continue
    echo "  removing bound token secret/${s}"
    kubectl -n "$NS" delete --ignore-not-found "secret/${s}"
  done

  # The local credential is now useless; leaving it behind is how a stale token
  # gets handed to somebody next week.
  CFG="${HERE}/${NS}-${n}.kubeconfig"
  if [ -f "$CFG" ]; then
    rm -f "$CFG"
    echo "  removed ${CFG}"
  fi
done

# --- prove it is gone ------------------------------------------------------
# Tokens minted by `kubectl create token` are bound to the ServiceAccount's UID,
# so deleting the SA invalidates every one of them immediately - there is no
# separate revocation step, but it is worth showing rather than asserting.
echo
echo "==> remaining in ${NS}:"
LEFT=$(kubectl -n "$NS" get serviceaccounts -l "$LABEL" \
        -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null || true)
if [ -n "$LEFT" ]; then
  printf '%s\n' "$LEFT" | sed 's/^/  /'
else
  echo "  no accounts created by this script"
fi
echo "done. Any token issued to a deleted account is invalid as of now."
