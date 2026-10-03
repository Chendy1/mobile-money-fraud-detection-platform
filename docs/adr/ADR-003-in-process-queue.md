# ADR-003: In-process queue instead of Redpanda/Kafka
**Context.** The machine has 8 GB RAM; Docker already takes a large share. A broker adds memory and operational cost.
**Decision.** Simulate the event stream with an in-process queue behind a small interface, and implement retries, duplicate events, idempotency and latency measurement on top.
**Alternatives.** Redpanda or Kafka (realistic, but too heavy locally); Redis Streams (lighter, still another service).
**Consequences.** Idempotency and failure handling are still demonstrated. We do not claim broker-level guarantees (partitions, durability). Swapping in Redpanda or Kinesis later is the documented migration path.
