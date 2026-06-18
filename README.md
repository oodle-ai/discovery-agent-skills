# Oodle Discovery Agent Skills

<p align="center"><strong>Runs entirely in your environment · Read-only operations only · No data shared externally</strong></p>

Automated tech stack and observability discovery for AI coding agents. Install the skill, run a single prompt, and get a verifiable HTML report of your infrastructure, observability tools, scale, observability costs, and pain points.

## What It Does

The discovery agent systematically examines your environment to produce a tailored report covering:

- **Environments** — Cloud accounts, Kubernetes clusters, regions, dev/staging/prod
- **Tech Stack** — Languages, frameworks, databases, message queues, caches
- **Infrastructure** — Compute scale and telemetry-relevant managed services (broad inventory only)
- **Observability Stack** — Monitoring, logging, tracing, alerting tools
- **Scale** — Telemetry volumes (metrics, log GB/day, trace ingestion) measured by deterministic collector scripts
- **Costs** — Observability spend only (vendor usage/billing APIs); never your overall cloud bill
- **Pain Points** — Observability-specific: alert fatigue, coverage gaps, tool sprawl, cost concerns

The agent presents a plan before proceeding, asks clarifying questions when it can't find information programmatically, and never performs destructive operations.

## How figures stay accurate

Detection is agent-driven, but **every volume and cost figure in the report is computed by a
deterministic collector script** (`collectors/<tool>/collect.py`) — not by the AI agent:

- Collectors query the same authoritative APIs that back each vendor's own usage/billing pages
  (e.g., the Datadog hourly usage and estimated-cost APIs).
- Every raw API response is saved (with credentials redacted) under `discovery-output/<tool>/evidence/`,
  and every figure records the endpoint, query, derivation method, and time window that produced it.
- Anything that couldn't be collected appears in the report's **Coverage & Gaps** section with the
  reason (e.g., `permission_denied`) and a remediation — never silently omitted, never guessed.
- The report itself is rendered by `report/generate_report.py`; the agent cannot edit figures.

Collector status: **Datadog**, **AWS CloudWatch**, **GCP Cloud Operations**, **Elasticsearch**, **OpenSearch**, **Grafana Mimir**, and **Kubernetes inventory** (nodes/vCPU/memory/services via kubectl) are available today. Prometheus/Thanos/VictoriaMetrics/Loki/Tempo
collectors are in progress;
until they ship, those tools are covered by user-reported numbers, clearly marked as unverified.

## Install

### Claude Code / Gemini CLI / Codex / Windsurf

```bash
npx skills add -g oodle-ai/discovery-agent-skills -y
```

### Cursor

```bash
npx skills add -g oodle-ai/discovery-agent-skills --agent cursor -y
```

### Manual

```bash
git clone https://github.com/oodle-ai/discovery-agent-skills.git
cp -r discovery-agent-skills/skills/* ~/.<agent>/skills/
```

## Usage

After installing, tell your coding agent:

```
Run the oodle-discovery skill
```

Or simply:

```
Discover my tech stack and observability setup and generate a report
```

The agent will:
1. Present a discovery plan for your approval
2. Run read-only commands to discover your environment
3. Run the matching collector script for each observability tool it finds (asking for read-only API credentials where needed)
4. Ask clarifying questions for anything it can't measure automatically
5. Generate a self-contained HTML report, walk you through any coverage gaps, and open it in your browser

## Safety

- **Read-only** — Never modifies, creates, or deletes any resources
- **Rate-limited** — Throttles API calls to avoid overwhelming systems; collectors self-throttle with circuit breakers
- **Credentials stay local** — Passed to collectors via environment variables, redacted from all saved output
- **Transparent** — Shows you the plan before executing; every figure links to its raw API evidence
- **Graceful** — Skips checks it can't perform (missing tools, no credentials) and reports gaps explicitly

## Output

The report is a single self-contained HTML file (no external dependencies) saved to `./discovery-report.html` and opened in your default browser. It includes:

- Executive summary with measured scale and observability-spend figures
- Per-environment breakdown and tech-stack tags
- **Coverage & Gaps** — what could not be measured and how to fix it
- Collapsible per-tool deep dives
- **Provenance appendix** — every figure mapped to its source API, query, and evidence file

Raw evidence lives in `./discovery-output/` so any figure can be re-derived offline
(`uv run collectors/<tool>/collect.py --report-only --output-dir ./discovery-output/<tool>`).

**See a sample Datadog report:** [preview it in your browser](https://htmlpreview.github.io/?https://github.com/oodle-ai/discovery-agent-skills/blob/main/examples/sample-datadog-report.html) ([source](examples/sample-datadog-report.html)) — generated from the synthetic test fixtures in this repo.

**See a sample CloudWatch report:** [preview it in your browser](https://htmlpreview.github.io/?https://github.com/oodle-ai/discovery-agent-skills/blob/main/examples/sample-cloudwatch-report.html) ([source](examples/sample-cloudwatch-report.html)) — generated from the synthetic test fixtures in this repo.

**See a sample GCP Cloud Operations report:** [preview it in your browser](https://htmlpreview.github.io/?https://github.com/oodle-ai/discovery-agent-skills/blob/main/examples/sample-gcp-report.html) ([source](examples/sample-gcp-report.html)) — generated from the synthetic test fixtures in this repo.

**See a sample Elasticsearch report:** [preview it in your browser](https://htmlpreview.github.io/?https://github.com/oodle-ai/discovery-agent-skills/blob/main/examples/sample-elasticsearch-report.html) ([source](examples/sample-elasticsearch-report.html)) — generated from the synthetic test fixtures in this repo.

**See a sample OpenSearch report:** [preview it in your browser](https://htmlpreview.github.io/?https://github.com/oodle-ai/discovery-agent-skills/blob/main/examples/sample-opensearch-report.html) ([source](examples/sample-opensearch-report.html)) — generated from the synthetic test fixtures in this repo.

**See a sample Mimir report:** [preview it in your browser](https://htmlpreview.github.io/?https://github.com/oodle-ai/discovery-agent-skills/blob/main/examples/sample-mimir-report.html) ([source](examples/sample-mimir-report.html)) — generated from the synthetic test fixtures in this repo.

## Requirements

- `uv` (https://docs.astral.sh/uv/) and Python ≥ 3.11 — used to run collector scripts. If unavailable, the skill runs in degraded mode (no measured figures, gaps reported).

Optional, used when present:

- `kubectl` — Kubernetes cluster discovery
- `aws` / `gcloud` / `az` CLIs — cloud environment discovery
- Read-only API keys for your observability vendors (e.g., Datadog API + application key with `usage_read`)
- Access to your code repository (for IaC and dependency detection)

## Development

```bash
uv sync --group dev
uv run pytest tests
uvx ruff check collectors report tests
```

Collector output contract: [schemas/summary.schema.json](schemas/summary.schema.json).
Agent context contract: [schemas/context.schema.json](schemas/context.schema.json).
Each collector documents its figure ↔ API mapping in its own README
(e.g., [collectors/datadog/README.md](collectors/datadog/README.md)).

## Platform Plugin Notes

This repository includes plugin metadata files for multiple agent platforms:

- `.claude-plugin/plugin.json` and `.cursor-plugin/plugin.json` — Identical metadata for Claude Code and Cursor respectively. These files must be kept in sync.
- `gemini-extension.json` — Minimal metadata for Gemini CLI. Gemini's extension schema supports only `name`, `version`, and `description`.

## License

[MIT](LICENSE)
