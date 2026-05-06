# Oodle Discovery Agent Skills

Automated tech stack and observability discovery for AI coding agents. Install the skill, run a single prompt, and get a comprehensive HTML report of your infrastructure, observability tools, scale, costs, and pain points.

## What It Does

The discovery agent systematically examines your environment to produce a tailored report covering:

- **Environments** — Cloud accounts, Kubernetes clusters, regions, dev/staging/prod
- **Tech Stack** — Languages, frameworks, databases, message queues, caches
- **Infrastructure** — Compute, networking, storage, IaC
- **Observability Stack** — Monitoring, logging, tracing, alerting tools
- **Scale** — Request rates, data volumes, node/pod counts
- **Costs** — Cloud spend, observability tool costs
- **Pain Points** — Observability-specific: alert fatigue, coverage gaps, tool sprawl, cost concerns

The agent presents a plan before proceeding, asks clarifying questions when it can't find information programmatically, and never performs destructive operations.

## Install

### Claude Code / Gemini CLI / Codex / Windsurf

```bash
npx skills add oodle-ai/discovery-agent-skills -y
```

### Cursor

```bash
npx skills add oodle-ai/discovery-agent-skills --agent cursor -y
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
3. Ask clarifying questions for anything it can't find automatically
4. Generate a self-contained HTML report and open it in your browser

## Safety

- **Read-only** — Never modifies, creates, or deletes any resources
- **Rate-limited** — Throttles API calls to avoid overwhelming systems
- **Transparent** — Shows you the plan before executing
- **Graceful** — Skips checks it can't perform (missing tools, no credentials) and notes gaps

## Output

The report is a single self-contained HTML file (no external dependencies) saved to `./discovery-report.html` in your current working directory and opened in your default browser. It includes:

- Executive summary with key metrics
- Per-environment breakdown
- Visual tags for technologies
- Pain points highlighted with recommendations
- Professional styling suitable for sharing

**See a sample report:** [examples/sample-report.html](examples/sample-report.html) — open it in your browser to see the expected format and level of detail.

## Requirements

The skill works best when run from a machine with access to your infrastructure. Common tools it leverages (all optional — it skips what's unavailable):

- `kubectl` — Kubernetes cluster discovery
- `aws` CLI — AWS resource and cost discovery
- `gcloud` CLI — GCP resource discovery
- `az` CLI — Azure resource discovery
- Access to your code repository (for IaC and dependency detection)

## Platform Plugin Notes

This repository includes plugin metadata files for multiple agent platforms:

- `.claude-plugin/plugin.json` and `.cursor-plugin/plugin.json` — Identical metadata for Claude Code and Cursor respectively. These files must be kept in sync.
- `gemini-extension.json` — Minimal metadata for Gemini CLI. Gemini's extension schema supports only `name`, `version`, and `description`.

## License

[MIT](LICENSE)
