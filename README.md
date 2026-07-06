# AI Platform Generator

Standalone AI generation service extracted from `ff-ai-platform`.

This repository contains:

- `services/ai_generation_service`: FastAPI service for AI generation orchestration.
- `docker-compose.yml`: standalone Docker compose for the AI generation service.
- `docs/ai-generation-service-api.md`: API contract and real Docker generation test record.
- `docs/platform-hermes-interaction-interfaces.md`: platform and Hermes interaction flow.

The service is responsible for:

- Creating AI generation runs.
- Calling Hermes Agent `/v1/runs`.
- Preparing shared work-order directories.
- Recovering generated artifacts.
- Managing skills.
- Running lightweight artifact quality checks.
- Optional OpenAI-compatible proxy and RAGFlow search.

It does not include Hermes itself. Run a compatible Hermes Agent separately and point `HERMES_RUNS_BASE_URL` to it.

## Quick Start

Copy env template:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and set Hermes values:

```text
HERMES_RUNS_BASE_URL=http://host.docker.internal:18642
HERMES_AGENT_API_KEY=hermes-local-dev-key
HERMES_AGENT_MODEL=gpt-5.5
```

Build and start:

```powershell
docker compose up -d --build
```

Health check:

```powershell
Invoke-RestMethod http://localhost:8091/health
Invoke-RestMethod http://localhost:8091/api/ai/runtime/status
```

## Docker Image

Build manually:

```powershell
docker build -f services/ai_generation_service/Dockerfile -t ai-generation-service:latest .
```

Run manually:

```powershell
docker run -d --name ai-generation-service `
  -p 8091:8091 `
  -e HERMES_RUNS_BASE_URL=http://host.docker.internal:18642 `
  -e HERMES_AGENT_API_KEY=hermes-local-dev-key `
  ai-generation-service:latest
```

## Notes

Docker image tar files, runtime `.env` files, and key-bearing Hermes packages are intentionally not committed to Git.

