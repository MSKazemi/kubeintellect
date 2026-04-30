#!/usr/bin/env bash
set -e
kubectl create namespace scenario-test --dry-run=client -o yaml | kubectl apply -f -
for i in 1 2 3; do
  kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: completed-job-${i}
  namespace: scenario-test
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: worker
          image: busybox:1.36
          command: ["sh", "-c", "echo done-${i}"]
EOF
done
kubectl wait job/completed-job-1 job/completed-job-2 job/completed-job-3 \
  -n scenario-test --for=condition=Complete --timeout=60s
