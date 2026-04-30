#!/usr/bin/env bash
set -e
# Service must have at least one ready endpoint
addrs=$(kubectl get endpoints backend-service -n scenario-test \
  -o jsonpath='{.subsets[0].addresses[0].ip}' 2>/dev/null)
[ -n "$addrs" ]
