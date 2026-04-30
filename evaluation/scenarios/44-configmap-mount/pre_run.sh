#!/usr/bin/env bash
set -e
kubectl create namespace scenario-test --dry-run=client -o yaml | kubectl apply -f -
kubectl delete configmap app-config -n scenario-test --ignore-not-found
kubectl delete deployment busybox-config -n scenario-test --ignore-not-found
