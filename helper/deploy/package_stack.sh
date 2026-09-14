#!/usr/bin/env sh
# Package the helper Terraform as a Resource Manager stack zip and optionally create the stack.
#
#   ./package_stack.sh                       -> <repo root>/vc-oci-helper-stack.zip
#                                               (committed; the README "Deploy to Oracle Cloud" button links to it)
#   ./package_stack.sh --create <compartment-ocid> [--name vc-oci-helper]   -> creates the stack via OCI CLI
#
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="${HERE}/terraform"
ROOT="$(cd "${HERE}/../.." && pwd)"
ZIP="${ROOT}/vc-oci-helper-stack.zip"
NAME="vc-oci-helper"
CREATE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --create) CREATE="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done

rm -f "$ZIP"
(cd "$SRC" && zip -q -r "$ZIP" main.tf variables.tf outputs.tf schema.yaml cloud-init.yaml)
echo "wrote $ZIP"

if [ -n "$CREATE" ]; then
  oci resource-manager stack create \
    --compartment-id "$CREATE" \
    --display-name "$NAME" \
    --description "vCenter to OCI export helper VM" \
    --config-source "$ZIP" \
    --terraform-version "1.5.x" \
    --query 'data.id' --raw-output
  echo "Stack created. Set the variables and run a plan/apply job in the console, or:"
  echo "  oci resource-manager job create-apply-job --stack-id <id> --execution-plan-strategy AUTO_APPROVED"
fi
