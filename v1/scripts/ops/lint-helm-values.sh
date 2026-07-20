#!/bin/bash
# Lint check: ensure every app defined in values-kind.yaml is also present in values-kind-dev.yaml.
#
# Helm replaces arrays entirely when merging -f files, so values-kind-dev.yaml must list
# every app from values-kind.yaml or that app will silently disappear on dev deploys.
#
# Usage:
#   bash scripts/ops/lint-helm-values.sh
#   make lint-helm-values

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

BASE="charts/kubeintellect/values-kind.yaml"
OVERLAY="charts/kubeintellect/values-kind-dev.yaml"

# Extract app names from the `apps:` block only.
# Strategy: print lines between "^apps:" and the next top-level key (line starting with a non-space char after apps:).
extract_app_names() {
    local file="$1"
    awk '
        /^apps:/ { in_apps=1; next }
        in_apps && /^[^ \t]/ { exit }
        in_apps && /^  - name:/ { print $3 }
    ' "$file"
}

base_apps=$(extract_app_names "$BASE")
overlay_apps=$(extract_app_names "$OVERLAY")

missing=()
for app in $base_apps; do
    if ! echo "$overlay_apps" | grep -qx "$app"; then
        missing+=("$app")
    fi
done

echo "Checking Helm values drift: $BASE vs $OVERLAY"
echo ""
echo "Apps in $BASE:"
for app in $base_apps; do echo "  - $app"; done
echo ""
echo "Apps in $OVERLAY:"
for app in $overlay_apps; do echo "  - $app"; done
echo ""

if [ ${#missing[@]} -gt 0 ]; then
    echo -e "${RED}FAIL: The following apps are in values-kind.yaml but MISSING from values-kind-dev.yaml:${NC}"
    for app in "${missing[@]}"; do
        echo -e "  ${YELLOW}✗ $app${NC}"
    done
    echo ""
    echo "Helm will silently drop these apps on dev deploys. Add them to values-kind-dev.yaml."
    exit 1
else
    echo -e "${GREEN}OK: All apps in values-kind.yaml are present in values-kind-dev.yaml.${NC}"
fi
