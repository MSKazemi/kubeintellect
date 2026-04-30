#!/usr/bin/env bash
set -e
kubectl get svc nginx -n scenario-test
kubectl get endpoints nginx -n scenario-test -o jsonpath='{.subsets[0].addresses}' | grep -v '^$'
