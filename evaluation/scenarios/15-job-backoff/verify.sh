#!/usr/bin/env bash
set -e
# Job must have at least one successful completion
succeeded=$(kubectl get job data-migration -n scenario-test \
  -o jsonpath='{.status.succeeded}' 2>/dev/null)
[ "${succeeded:-0}" -ge 1 ]
