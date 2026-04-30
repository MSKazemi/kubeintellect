#!/usr/bin/env bash
set -e
kubectl create namespace scenario-test --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f - <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: error-pod
  namespace: scenario-test
  labels:
    app: error-pod
spec:
  replicas: 1
  selector:
    matchLabels:
      app: error-pod
  template:
    metadata:
      labels:
        app: error-pod
    spec:
      containers:
        - name: app
          image: busybox:1.36
          command: ["sh", "-c", "exit 1"]
          resources:
            limits: {cpu: 50m, memory: 32Mi}
EOF
sleep 15
