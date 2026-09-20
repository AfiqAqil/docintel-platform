# Document Intelligence Platform

An internal AI document intelligence platform. Business users upload documents; the platform
classifies them, extracts structured information, validates it, flags what is missing, and
produces a report. Processing is asynchronous and event driven, orchestrated with LangGraph,
running on AWS ECS Fargate, provisioned entirely with Terraform.

**Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) first.** It is the design document and
the source of truth for this repository: the end to end sequence, the LangGraph workflow and
state model, the AWS service choices, the networking and private DNS design, the Terraform
structure, and the trade offs behind each decision.

[`docs/BUILD_LOG.md`](docs/BUILD_LOG.md) records what was built in which order, how each part
was verified, and every decision that the architecture document did not already cover.

## The three services

| Service | Runtime | Reachable from | Job |
|---|---|---|---|
| Frontend | nginx plus a React SPA, port 8080 | The load balancer | Upload, status, history, reports |
| Backend API | FastAPI, port 8000 | The frontend only, over private DNS | Intake, metadata, status, reports |
| AI Processing | Python plus LangGraph, no listener | Nothing | Consumes SQS, runs the graph, records the outcome |

## Repository layout

```
backend/    FastAPI service, SQLAlchemy models, Alembic migrations
worker/     AI processing service
              consumer/   SQS polling, claim, lease heartbeat, ack rules, reaper
              graph/      LangGraph nodes, routers, shared state
              rules/      deterministic logic: parsing, validation, masking
              llm/        provider selection, prompts, structured output schemas
frontend/   React plus Vite SPA, served by nginx
samples/    generator for the synthetic sample documents
infra/      Terraform: bootstrap stack, then the main stack
docs/       architecture, build log, evidence from the deployed environment
```

The four directories inside `worker/` are deliberate. They separate infrastructure event
handling, workflow orchestration, deterministic application logic, and LLM assisted
processing, which is a distinction the assignment asks for explicitly.

## Status

Under construction. Provisioning and local development instructions land with the deployment,
in the phase the build log calls phase 11.
