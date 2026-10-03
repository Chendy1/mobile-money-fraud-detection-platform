# ADR-005: Docker Compose instead of Kubernetes
**Context.** One developer, one machine, a handful of services.
**Decision.** Use Docker Compose for local orchestration.
**Alternatives.** Kubernetes (kind/minikube): large RAM overhead, no problem here that it solves.
**Consequences.** Simple, one-command startup. Does not demonstrate Kubernetes skills; the cloud design uses ECS Fargate and documents why EKS is unnecessary.
