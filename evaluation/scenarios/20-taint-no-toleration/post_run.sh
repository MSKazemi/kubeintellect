#!/usr/bin/env bash
# Remove the taint after the scenario so it does not affect other tests.
kubectl taint nodes testbed-v2-worker dedicated=gpu:NoSchedule- 2>/dev/null || true
