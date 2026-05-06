---
name: oodle-discovery
description: Discover a company's tech stack, observability stack, infrastructure scale, costs, and pain points. Produces a comprehensive HTML report.
metadata:
  version: "1.0.0"
  author: oodle-ai
  repository: https://github.com/oodle-ai/discovery-agent-skills
  tags: oodle,discovery,observability,tech-stack,infrastructure,assessment
  alwaysApply: "false"
---

# Oodle Discovery Agent

Automated tech stack and observability discovery. Run this skill to produce a comprehensive HTML report of your infrastructure, observability tools, scale, costs, and pain points.

## Install

### Claude Code
```bash
npx skills add oodle-ai/discovery-agent-skills -y
```

### Cursor
```bash
npx skills add oodle-ai/discovery-agent-skills --target-agent cursor -y
```

### Gemini CLI / Codex / Windsurf
```bash
npx skills add oodle-ai/discovery-agent-skills -y
```

## Usage

After installing, tell your coding agent:

```
Run the oodle-discovery skill
```

The agent will present a discovery plan, ask for approval, then systematically discover your environment and produce an HTML report.
