#!/usr/bin/env bash
set -e
for deploy in $(kubectl get deploy -n scenario-test -o jsonpath='{.items[*].metadata.name}'); do
  replicas=$(kubectl get deploy "$deploy" -n scenario-test -o jsonpath='{.spec.replicas}')
  if [ "$replicas" != "0" ]; then
    echo "FAIL: $deploy still has $replicas replicas"
    exit 1
  fi
done
echo "All deployments scaled to 0"
