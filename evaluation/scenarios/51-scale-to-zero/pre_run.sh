#!/usr/bin/env bash
set -e
kubectl create namespace scenario-test --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f - <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: workload-a
  namespace: scenario-test
  labels:
    app: workload-a
spec:
  replicas: 2
  selector:
    matchLabels:
      app: workload-a
  template:
    metadata:
      labels:
        app: workload-a
    spec:
      containers:
        - name: app
          image: busybox:1.36
          command: ["sleep", "3600"]
          resources:
            limits: {cpu: 50m, memory: 32Mi}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: workload-b
  namespace: scenario-test
  labels:
    app: workload-b
spec:
  replicas: 3
  selector:
    matchLabels:
      app: workload-b
  template:
    metadata:
      labels:
        app: workload-b
    spec:
      containers:
        - name: app
          image: busybox:1.36
          command: ["sleep", "3600"]
          resources:
            limits: {cpu: 50m, memory: 32Mi}
EOF
