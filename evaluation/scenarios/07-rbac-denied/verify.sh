#!/usr/bin/env bash
set -e
# Role or ClusterRole binding must exist for limited-sa
kubectl get rolebinding -n scenario-test \
  -o jsonpath='{.items[*].subjects[?(@.name=="limited-sa")].name}' \
  | grep -q "limited-sa"
