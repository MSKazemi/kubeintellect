#!/usr/bin/env bash
# Ensure loop-test is gone before the scenario runs so the agent creates it fresh.
kubectl delete namespace loop-test --ignore-not-found --wait=false
