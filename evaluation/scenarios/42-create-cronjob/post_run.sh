#!/usr/bin/env bash
kubectl delete cronjob log-cleaner -n scenario-test --ignore-not-found
kubectl delete namespace scenario-test --ignore-not-found
