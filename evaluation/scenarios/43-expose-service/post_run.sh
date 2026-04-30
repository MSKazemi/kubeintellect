#!/usr/bin/env bash
kubectl delete service nginx -n scenario-test --ignore-not-found
kubectl delete deployment nginx -n scenario-test --ignore-not-found
kubectl delete namespace scenario-test --ignore-not-found
