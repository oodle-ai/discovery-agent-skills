---
name: oodle-onboarding
description: Integrate infrastructure with Oodle observability platform. Guides the agent through listing available integrations, fetching setup specs, and executing step-by-step installation for any integration type (Kubernetes, AWS, GCP, etc.).
metadata:
  version: "1.0.0"
  author: oodle-ai
  repository: https://github.com/oodle-ai/discovery-agent-skills
  tags: oodle,onboarding,integration,kubernetes,observability,setup
  globs: ""
  alwaysApply: "false"
---

# Oodle Integration Onboarding

This skill guides the agent through integrating infrastructure with the Oodle observability platform. Given a prompt like "Integrate my Kubernetes cluster with Oodle", the agent will fetch the setup specification from Oodle's API and walk through every step to completion.

For install instructions, see [README.md](README.md).

## Principles

1. **Setup spec is the source of truth.** Always fetch the latest setup spec from the API before executing any steps. Never hardcode integration instructions — they change as the platform evolves.
2. **Verify each step.** After executing each setup step, verify it succeeded before moving to the next.
3. **Ask when uncertain.** If a required parameter is not obvious from the environment, ask the user.
4. **Non-destructive by default.** Always show the user what you are about to do and get confirmation before creating or modifying resources (Helm installs, config changes, etc.).
5. **Idempotent.** If a step has already been completed (e.g., namespace already exists, Helm chart already installed), skip it and note that it was already done.

## Prerequisites

The `oodle` CLI must be available. If it is not installed, install it:

```bash
# Check if oodle is available
which oodle 2>/dev/null || command -v oodle 2>/dev/null
```

If not found, install via one of these methods (try in order):

```bash
# Homebrew (macOS / Linux)
brew tap oodle-ai/oodle && brew install oodle

# Or: go install
go install github.com/oodle-ai/oodle-cli/cmd/oodle@latest

# Or: download from GitHub releases
# Visit https://github.com/oodle-ai/oodle-cli/releases
```

Verify the CLI is working:

```bash
oodle version
```

### Authentication

The `oodle` CLI needs to be configured with API credentials. Check if it is already configured by running an authenticated command:

```bash
oodle integrations list -o json 2>/dev/null
```

If this fails with an authentication error, the user needs to set up credentials. There are three options:

```bash
# Option 1: Interactive configuration
oodle configure

# Option 2: OAuth login (interactive browser flow)
oodle auth login

# Option 3: Environment variables
export OODLE_API_KEY="<api-key>"
export OODLE_INSTANCE="<instance-id>"
```

**Note:** The `get-setup-spec` command does NOT require authentication, so you can fetch setup instructions even before the CLI is fully configured. However, `integrations list` and the actual integration setup will require valid credentials. If the user already knows the integration type and auth is not yet configured, you can skip ahead to Phase 2 to fetch the setup spec, then return to configure auth before Phase 5 (execution).

## Execution Flow

### Phase 0: Understand the Request

Parse the user's request to determine:
- **Integration type** — What they want to integrate (e.g., "kubernetes", "aws-cloudwatch", "datadog"). If unclear, proceed to Phase 1 to list available integrations and ask the user to choose.
- **Scope** — Any specific details (e.g., cluster name, region, namespace).

### Phase 1: List Available Integrations

Fetch the list of available integrations to validate the requested type and show the user what's available:

```bash
oodle integrations list -o json
```

This returns a JSON array of integration objects with fields like `name`, `type`, `status`, and `categories`.

**Note:** This command requires authentication. If it fails due to missing credentials, either guide the user through auth setup first (see Prerequisites → Authentication), or — if the user already specified an integration type — skip ahead to Phase 2 (`get-setup-spec` does not require auth) and return to configure auth before Phase 5.

**If the user didn't specify an integration type**, present the list and ask them to choose:

```
Here are the available integrations:

| Name | Type | Status | Categories |
|------|------|--------|------------|
| ...  | ...  | ...    | ...        |

Which integration would you like to set up?
```

**If the user specified a type**, confirm it exists in the list. If not, show the available options and ask for clarification.

### Phase 2: Fetch the Setup Specification

Once you know the integration type, fetch its setup spec:

```bash
oodle integrations get-setup-spec <integration-type> -o json
```

For example:

```bash
oodle integrations get-setup-spec kubernetes -o json
```

This returns a structured JSON object containing:
- **requirements** — Prerequisites (tools, access, permissions) needed before starting
- **parameters** — Configuration values needed (some required, some optional)
- **setup_methods** — One or more methods to install/configure the integration, each with step-by-step instructions
- **config_templates** — Configuration file templates with placeholder values
- **validation** — How to verify the integration is working

**Parse this response carefully.** The setup spec is the authoritative guide for the rest of the process.

### Phase 3: Check Requirements

Before starting the setup, verify all requirements from the spec are met:

```bash
# Example: for a Kubernetes integration, check kubectl access
kubectl cluster-info 2>/dev/null
kubectl auth can-i create namespace 2>/dev/null

# Example: check if Helm is available (if required)
helm version 2>/dev/null
```

For each requirement:
- If met, note it and continue.
- If not met, tell the user what's missing and how to fix it. Wait for confirmation before proceeding.

### Phase 4: Collect Parameters

The setup spec defines required and optional parameters. Resolve them in this order:

1. **From the environment** — Check if values are already available (e.g., cluster name from `kubectl config current-context`, instance ID from `OODLE_INSTANCE` env var or CLI config).
2. **From the user's request** — The user may have specified values in their prompt (e.g., "integrate my production cluster").
3. **Ask the user** — For any remaining required parameters, ask the user. Group questions together rather than asking one at a time.

Example:

```
I need a few values to configure the integration:

1. **Cluster name**: I detected `prod-us-east-1` from your current kubectl context. Is that correct?
2. **Oodle API key**: Found in your CLI config. ✓
3. **Namespace**: The spec suggests `oodle-system`. Shall I use that, or do you prefer a different namespace?
```

### Phase 5: Execute Setup

Follow the setup method from the spec step by step. **Always show the user what you're about to do and get confirmation before executing commands that create or modify resources.**

For each step in the setup method:

1. **Show the command** — Display the exact command you will run, with all parameters filled in.
2. **Get confirmation** — Ask the user to confirm before executing (unless it's a read-only check).
3. **Execute** — Run the command.
4. **Verify** — Check the output for success. If the step failed, diagnose the issue and suggest a fix.

> **Note:** The steps below are illustrative only. Always follow the actual steps from the setup spec fetched in Phase 2.

Example flow for a Kubernetes integration:

```
Step 1/4: Create the oodle-system namespace

I'll run:
  kubectl create namespace oodle-system

Shall I proceed? [Y/n]
```

```
Step 2/4: Add the Oodle Helm repository

I'll run:
  helm repo add oodle https://charts.oodle.ai
  helm repo update

Shall I proceed? [Y/n]
```

```
Step 3/4: Install the Oodle collector via Helm

I'll run:
  helm install oodle-collector oodle/oodle-collector \
    --namespace oodle-system \
    --set apiKey=<redacted> \
    --set clusterName=prod-us-east-1 \
    --set instance=<instance-id>

Shall I proceed? [Y/n]
```

```
Step 4/4: Verify the collector is running

I'll run:
  kubectl get pods -n oodle-system
  kubectl logs -n oodle-system -l app=oodle-collector --tail=20

Shall I proceed? [Y/n]
```

**If the spec provides multiple setup methods** (e.g., Helm vs. raw manifests), present the options to the user and let them choose. Default to the first/recommended method.

**If the spec includes config templates**, fill in the parameter values and show the rendered config to the user before applying it.

### Phase 6: Validate the Integration

After setup is complete, run the validation steps from the spec:

```bash
# Verify integration appears in the list
oodle integrations list -o json
```

Check that the integration now shows with an `active` (or equivalent) status.

If the spec includes additional validation commands (e.g., checking that metrics are flowing), run those too:

```bash
# Example: verify data is being received (wait a minute for data to flow)
# The specific commands depend on the setup spec's validation section
```

### Phase 7: Summary

Present a summary of what was done:

```
Integration complete! Here's what was set up:

- **Type**: Kubernetes
- **Cluster**: prod-us-east-1
- **Namespace**: oodle-system
- **Components installed**: oodle-collector (Helm chart v1.2.3)
- **Status**: Active — metrics are flowing to Oodle

You can check the integration status anytime with:
  oodle integrations list
```

## Failure Handling

| Situation | Action |
|-----------|--------|
| `oodle` CLI not installed | Guide user through installation (see Prerequisites) |
| CLI not configured | Guide user through `oodle configure` or env vars |
| Integration type not found | Show available integrations, ask user to choose |
| `get-setup-spec` returns 404 | The integration type doesn't exist. Show available types. |
| Requirement not met (e.g., no kubectl) | Tell user what's missing, provide install instructions, wait |
| Setup step fails | Show the error, diagnose the issue, suggest a fix. Do not continue to the next step. |
| Helm install fails | Check for existing releases (`helm list -n <ns>`), suggest `helm upgrade` if already installed |
| Namespace already exists | Skip creation, note it was already present |
| Validation fails | Wait 60 seconds and retry. If still failing, check logs and present diagnostics to the user. |
| Timeout waiting for pods | Increase wait time, check events with `kubectl describe pod`, present findings |

## Safety Rules

- **Always confirm** before running commands that create, modify, or delete resources.
- **Never delete** existing resources unless explicitly asked by the user.
- **Redact secrets** in output — never display full API keys, tokens, or passwords. Show only the first/last 4 characters.
- **Idempotent operations** — If something already exists (namespace, Helm release, config), skip it rather than failing.
- **Clean up on failure** — If a multi-step setup fails partway through, tell the user what was created and how to clean it up if needed.

## Example Prompts

These are example prompts that should trigger this skill:

- "Integrate my Kubernetes cluster with Oodle"
- "Set up Oodle monitoring for my AWS account"
- "Connect my infrastructure to Oodle"
- "Onboard my cluster to Oodle observability"
- "Set up the Oodle collector on my k8s cluster"
- "What integrations does Oodle support?" (triggers Phase 1 only)
- "Show me how to integrate Datadog metrics with Oodle"

## References

- [Oodle CLI documentation](https://github.com/oodle-ai/oodle-cli)
- [Oodle](https://oodle.ai) — Observability platform
