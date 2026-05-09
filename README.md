# Oodle Agent Skills

AI agent skills for infrastructure discovery and Oodle platform onboarding. Install the skills, run a single prompt, and let your coding agent discover your tech stack or integrate your infrastructure with Oodle.

## Skills

### Discovery (`SKILL.md`)

Automated tech stack and observability discovery. The agent systematically examines your environment to produce a tailored HTML report covering:

- **Environments** — Cloud accounts, Kubernetes clusters, regions, dev/staging/prod
- **Tech Stack** — Languages, frameworks, databases, message queues, caches
- **Infrastructure** — Compute, networking, storage, IaC
- **Observability Stack** — Monitoring, logging, tracing, alerting tools
- **Scale** — Request rates, data volumes, node/pod counts
- **Costs** — Cloud spend, observability tool costs
- **Pain Points** — Observability-specific: alert fatigue, coverage gaps, tool sprawl, cost concerns

The agent presents a plan before proceeding, asks clarifying questions when it can't find information programmatically, and never performs destructive operations.

### Onboarding (`ONBOARDING.md`)

Guided integration of your infrastructure with the Oodle observability platform. The agent uses the Oodle CLI to fetch setup specifications and walks through every step to completion. Supports any integration type (Kubernetes, AWS, GCP, etc.).

Tell your agent:

```
Integrate my Kubernetes cluster with Oodle
```

The agent will:
1. Ensure the Oodle CLI is installed and configured
2. Fetch the setup specification for the requested integration type
3. Check prerequisites and collect required parameters
4. Execute the setup steps with your confirmation at each stage
5. Validate the integration is working

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

### Discovery

Tell your coding agent:

```
Run the oodle-discovery skill
```

Or simply:

```
Discover my tech stack and observability setup and generate a report
```

The discovery report is a single self-contained HTML file saved to `./discovery-report.html`. See [examples/sample-report.html](examples/sample-report.html) for the expected format.

### Onboarding

Tell your coding agent:

```
Integrate my Kubernetes cluster with Oodle
```

Or any integration type:

```
Set up Oodle monitoring for my AWS account
```

The agent will guide you through the full setup interactively, confirming each step before executing.

## Safety

- **Transparent** — Shows you the plan before executing
- **Confirmation required** — Onboarding asks for confirmation before creating or modifying resources
- **Discovery is read-only** — Never modifies, creates, or deletes any resources
- **Rate-limited** — Throttles API calls to avoid overwhelming systems
- **Graceful** — Skips checks it can't perform (missing tools, no credentials) and notes gaps

## Requirements

The skills work best when run from a machine with access to your infrastructure. Common tools they leverage (all optional — they skip what's unavailable):

- `oodle` CLI — Required for onboarding; install via `brew install oodle` or `go install github.com/oodle-ai/oodle-cli/cmd/oodle@latest`
- `kubectl` — Kubernetes cluster discovery and integration
- `helm` — Kubernetes integration setup (if using Helm method)
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
