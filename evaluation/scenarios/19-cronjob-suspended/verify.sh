#!/usr/bin/env bash
set -e
# CronJob must be un-suspended
suspended=$(kubectl get cronjob db-backup -n scenario-test \
  -o jsonpath='{.spec.suspend}' 2>/dev/null)
[ "${suspended}" != "true" ]
