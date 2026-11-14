# Roadmap
Each phase ends with tests, docs, a CHANGELOG entry, commits, and a stop for approval.

**MVP:** 0 Architecture; 1 Repo, environment, minimal CI; 2 Synthetic generator; 3 Ingestion and layers; 4 Data quality; 5 EDA (done); 6 Features (done); 7 Baseline models (done); 8 Advanced model and cost thresholds (done); 9 Explainability and model card (done); 10 MLflow and training command (done); 11 FastAPI (done); 12 Docker Compose (done).

**Productionization:** 13 Streaming simulation (done); 14 Monitoring (done); 15 Drift (done); 16 Gated retraining (done); 17 Dashboard (done); 18 Security hardening (done); 19 Load testing (done); 20 Production-like local deployment (done).

**Cloud (optional):** 21 AWS design (done, design only); 22 Terraform (plan-only, done); 23 Cloud deployment doc and cost controls (done).

**Delivery:** 24 Final docs (done); 25 Demo scripts (done); 26 Portfolio, resume, interview prep (done); 27 Evidence-based scorecard (done); 28 GitHub release (prepared; the tag and release are created by the author's commands in [releasing.md](releasing.md)).

Status: Phases 0 to 28 complete. Phases 2 to 28 were built and tested in the assistant's sandbox; CI (Ubuntu) runs the whole suite, builds the images and validates the Terraform; a subset was run on the developer's Windows machine. Exactly what, and what not: [verification-status.md](verification-status.md).
