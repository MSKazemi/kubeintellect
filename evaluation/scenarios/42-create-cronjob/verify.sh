#!/usr/bin/env bash
set -e
kubectl get cronjob log-cleaner -n scenario-test
