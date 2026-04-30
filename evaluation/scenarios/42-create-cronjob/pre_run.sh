#!/usr/bin/env bash
set -e
kubectl create namespace scenario-test --dry-run=client -o yaml | kubectl apply -f -
kubectl delete cronjob log-cleaner -n scenario-test --ignore-not-found
