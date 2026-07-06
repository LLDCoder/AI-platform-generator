# AI Generation Service

Standalone service for the AI generation part of ff-ai-platform.

It owns:

- Hermes `/v1/runs` orchestration.
- AI run state polling.
- Platform task-state callbacks.
- Skill CRUD/matching/export.
- Optional OpenAI-compatible model proxy.
- Lightweight quality checks for generated artifacts.

It does not own:

- Platform users, tenants, permissions, or UI.
- Work order management UI.
- Preview iframe UI.
- Long-term deployment registry.

## Local Docker Build

```powershell
docker build -f services/ai_generation_service/Dockerfile -t ff-ai-generation-service:local .
```

## Main Endpoints

```text
GET  /api/ai/health
GET  /api/ai/runtime/status
POST /api/ai/runs
GET  /api/ai/runs/{run_id}
GET  /api/ai/runs/{run_id}/events
POST /api/ai/runs/{run_id}/retry
POST /api/ai/runs/{run_id}/cancel

GET  /api/ai/skills
POST /api/ai/skills
GET  /api/ai/skills/{skill_id}
PATCH /api/ai/skills/{skill_id}
DELETE /api/ai/skills/{skill_id}
POST /api/ai/skills/match
POST /api/ai/skills/export/hermes
POST /api/ai/skills/reload
```

## Environment

```text
AI_GENERATION_API_KEY=
AI_GENERATION_DATA_DIR=/data
HERMES_RUNS_BASE_URL=http://hermes-agent:8642
HERMES_AGENT_API_KEY=hermes-local-dev-key
HERMES_AGENT_MODEL=gpt-5.5
PLATFORM_BASE_URL=http://backend:8001
PLATFORM_API_KEY=
OPENAI_BASE_URL=
OPENAI_API_KEY=
RAGFLOW_BASE_URL=http://host.docker.internal:9380
RAGFLOW_API_KEY=
```
