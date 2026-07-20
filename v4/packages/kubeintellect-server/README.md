# kubeintellect (server)

The KubeIntellect server — FastAPI app, agent graph, tools, memory, and the
`kubeintellect` CLI entry point. This is the `kubeintellect` distribution on
PyPI; it depends on its sibling workspace packages `kube-q` (the `kq` client)
and `ki-protocol` (the shared SSE wire protocol), so installing it gives the
full stack.

The Python module name remains `app` (V2 heritage); see the repo root README
for project documentation.
