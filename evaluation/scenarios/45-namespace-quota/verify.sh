#!/usr/bin/env bash
set -e
kubectl get namespace quota-test
kubectl get resourcequota -n quota-test -o jsonpath='{.items[0].spec.hard}' | grep '"pods":"10"'
