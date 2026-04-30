#!/usr/bin/env bash
set -e
kubectl create namespace scenario-test --dry-run=client -o yaml | kubectl apply -f -
kubectl delete service nginx -n scenario-test --ignore-not-found
kubectl apply -f - <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx
  namespace: scenario-test
  labels:
    app: nginx
spec:
  replicas: 1
  selector:
    matchLabels:
      app: nginx
  template:
    metadata:
      labels:
        app: nginx
    spec:
      containers:
        - name: nginx
          image: nginx:1.25
          ports:
            - containerPort: 80
EOF
kubectl wait deployment nginx -n scenario-test --for=condition=Available --timeout=60s
