#!/usr/bin/env bash
# Apply taint to worker node so the gpu-workload pod has no valid node to schedule on.
kubectl taint nodes testbed-v2-worker dedicated=gpu:NoSchedule --overwrite
