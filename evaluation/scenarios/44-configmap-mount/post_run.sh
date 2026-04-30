#!/usr/bin/env bash
kubectl delete deployment busybox-config -n scenario-test --ignore-not-found
kubectl delete configmap app-config -n scenario-test --ignore-not-found
kubectl delete namespace scenario-test --ignore-not-found
