# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

## 1.0.1 - 2026-08-05

### Delivery

- Refactored internal webhook client error handling behind a compatible, transport-neutral timeout
  and error contract while preserving delivery outcomes and persisted messages.

### Security design

- Completed and reviewed a design-only spike for an SSRF-safe DNS-to-connection delivery boundary.
- Clarified that production webhook delivery does not implement this SSRF boundary; enforcement
  remains deferred to follow-up implementation.

## 1.0.0 - 2026-07-28

### Ingestion and persistence

- Added endpoint configuration and durable PostgreSQL JSON webhook event ingestion.
- Added optional endpoint-scoped idempotency with atomic event and initial job persistence.

### Delivery and retry

- Added persistent event-wide delivery attempt history and synchronous HTTP execution.
- Added deterministic retry decisions with bounded exponential backoff and terminal states.

### Worker and recovery

- Added a separately started worker with bounded recovery and processing iterations.
- Added stale-processing recovery, explicit transaction boundaries, and graceful shutdown.

### Replay and inspection

- Added synchronous manual delivery and asynchronous terminal replay with a fresh retry-cycle
  budget.
- Added event-scoped job inspection, cursor-based job listing, and attempt listing.

### Operations

- Added dependency-free liveness, PostgreSQL readiness, and aggregate delivery queue inspection.

### Documentation

- Added development, database, delivery, API, operations, and architecture guides.

### Quality

- Added real PostgreSQL integration coverage, Alembic checks, Ruff, strict mypy, and GitHub Actions
  CI.

### Important limitations

- Delivery is effectively at-least-once, not exactly-once; uncertain remote outcomes can cause
  duplicate downstream side effects.
- Authentication and authorization are not built in.
- The worker runs separately from the API, without distributed coordination or heartbeat.
- Operational endpoints return point-in-time snapshots that can become stale immediately.
