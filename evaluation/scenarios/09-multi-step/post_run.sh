#!/usr/bin/env bash
# Safety cleanup — remove loop-test if the agent left it behind.
kubectl delete namespace loop-test --ignore-not-found --wait=false
