---
name: discovery
description: Discover a company's tech stack, observability stack, infrastructure scale, costs, and pain points across all environments. Produces a tailored HTML report.
metadata:
  version: "1.0.0"
  author: oodle-ai
  repository: https://github.com/oodle-ai/discovery-agent-skills
  tags: oodle,discovery,observability,tech-stack,infrastructure,assessment
  globs: ""
  alwaysApply: "false"
---

# Infrastructure & Observability Discovery

This skill guides the agent through a systematic discovery of a company's tech stack, observability stack, infrastructure scale, costs, and operational pain points. The output is a comprehensive HTML report opened in the user's browser.

## Principles

1. **Non-destructive only.** Never modify, delete, or write to any system. All operations are read-only.
2. **Rate-limited.** Wait 1-2 seconds between API calls. Never issue more than 5 requests per second to any single endpoint.
3. **Ask when uncertain.** If information cannot be discovered programmatically, ask the user.
4. **Plan first.** Always present the discovery plan and get user approval before executing.
5. **Multi-environment aware.** Discover all environments (dev, staging, prod, etc.) separately.

## Execution Flow

### Phase 0: Present Plan

Before doing anything, present this plan to the user and ask for approval:

```
I'll discover your infrastructure and observability setup. Here's my plan:

1. **Environment Detection** — Identify all environments (cloud accounts, k8s clusters, regions)
2. **Tech Stack Discovery** — Languages, frameworks, databases, message queues, caches
3. **Infrastructure Discovery** — Cloud provider, compute, networking, storage
4. **Observability Stack Discovery** — Monitoring, logging, tracing, alerting tools
5. **Scale Assessment** — Request rates, data volumes, node counts, resource utilization
6. **Cost Discovery** — Cloud spend, observability tool costs, license costs
7. **Pain Points** — Alert fatigue, gaps in coverage, toil, reliability issues

I will only perform read-only operations. No changes will be made to your systems.
Shall I proceed?
```

Wait for user confirmation before continuing.

### Phase 1: Environment Detection

Discover all environments by checking these sources in order. Stop each check after 5 seconds if no response.

#### 1.1 Kubernetes Clusters

```bash
# Check if kubectl is available and configured
kubectl config get-contexts 2>/dev/null

# For each context, get basic cluster info
kubectl cluster-info 2>/dev/null
kubectl get namespaces -o json 2>/dev/null | jq -r '.items[].metadata.name'
```

#### 1.2 Cloud Provider Detection

```bash
# AWS
aws sts get-caller-identity 2>/dev/null
aws organizations list-accounts 2>/dev/null
aws ec2 describe-regions --query 'Regions[].RegionName' -o text 2>/dev/null

# GCP
gcloud config list --format=json 2>/dev/null
gcloud projects list --format=json 2>/dev/null

# Azure
az account list -o json 2>/dev/null
```

#### 1.3 Infrastructure-as-Code Detection

```bash
# Look for IaC files in the current workspace
find . -maxdepth 4 -name "*.tf" -o -name "*.tfvars" | head -20
find . -maxdepth 4 -name "pulumi.*" -o -name "Pulumi.yaml" | head -10
find . -maxdepth 4 -name "cdk.json" -o -name "serverless.yml" | head -10
find . -maxdepth 4 -name "docker-compose*.yml" -o -name "docker-compose*.yaml" | head -10
find . -maxdepth 4 -name "helmfile.yaml" -o -name "Chart.yaml" | head -20

# Parse Terraform files for managed services (databases, queues, monitoring)
grep -rh 'resource "aws_\|resource "google_\|resource "azurerm_' --include="*.tf" . 2>/dev/null | sed 's/.*resource "\([^"]*\)".*/\1/' | sort | uniq -c | sort -rn | head -20
```

#### 1.4 CI/CD Detection

```bash
# Check for CI/CD configuration
ls -la .github/workflows/ 2>/dev/null
ls -la .gitlab-ci.yml 2>/dev/null
ls -la Jenkinsfile 2>/dev/null
ls -la .circleci/ 2>/dev/null
ls -la .buildkite/ 2>/dev/null
find . -maxdepth 3 -name "*.yaml" -path "*argocd*" | head -5
find . -maxdepth 3 -name "*.yaml" -path "*flux*" | head -5
```

#### 1.5 Docker & Non-Kubernetes Environments

```bash
# Docker Compose services
docker compose config --services 2>/dev/null || docker-compose config --services 2>/dev/null
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' 2>/dev/null | head -20

# System services (bare metal / VM)
systemctl list-units --type=service --state=running 2>/dev/null | grep -i "postgres\|mysql\|mongo\|redis\|nginx\|haproxy\|kafka\|rabbit\|elastic\|prometheus\|grafana\|docker" | head -20

# Process-based detection (fallback)
ps aux 2>/dev/null | grep -i "postgres\|mysql\|mongo\|redis\|nginx\|kafka\|rabbit\|elastic\|prometheus\|grafana\|java\|node\|python\|ruby\|go" | grep -v grep | head -20
```

#### 1.6 Helm Releases & CRDs

```bash
# Helm releases (reveals operator-managed stacks)
helm list -A -o json 2>/dev/null | jq -r '.[] | "\(.namespace)\t\(.name)\t\(.chart)"' | head -30

# CRDs (reveals operators installed)
kubectl get crd 2>/dev/null | grep -i "monitoring\|observability\|prometheus\|datadog\|elastic\|cert-manager\|istio\|linkerd" | head -20
```

If environments cannot be determined from the above, ask the user:
```
I couldn't automatically detect all your environments. Could you tell me:
- What cloud provider(s) do you use?
- How many environments do you have (dev/staging/prod)?
- Do you use Kubernetes? If so, how many clusters?
```

### Phase 2: Tech Stack Discovery

#### 2.1 Languages & Frameworks

```bash
# Detect from package files
find . -maxdepth 4 \( -name "package.json" -o -name "go.mod" -o -name "requirements.txt" \
  -o -name "Pipfile" -o -name "pyproject.toml" -o -name "Gemfile" -o -name "pom.xml" \
  -o -name "build.gradle" -o -name "Cargo.toml" -o -name "mix.exs" \
  -o -name "*.csproj" -o -name "composer.json" \) 2>/dev/null | head -30

# Read key dependency files to identify frameworks
# For each found file, read it to identify major frameworks
```

#### 2.2 Databases

```bash
# Check Kubernetes for database services
kubectl get services -A -o json 2>/dev/null | jq -r '.items[] | select(.metadata.name | test("postgres|mysql|mongo|redis|elastic|kafka|rabbit|cassandra|dynamo|cockroach|clickhouse")) | "\(.metadata.namespace)/\(.metadata.name)"'

# Check for database connection strings in config
find . -maxdepth 4 \( -name "*.env" -o -name "*.env.example" -o -name "application*.yml" \
  -o -name "config*.yaml" \) -exec grep -li "DATABASE\|REDIS\|MONGO\|KAFKA\|RABBIT\|ELASTIC" {} \; 2>/dev/null | head -10

# AWS managed databases
aws rds describe-db-instances --query 'DBInstances[].{Engine:Engine,Class:DBInstanceClass,ID:DBInstanceIdentifier}' -o table 2>/dev/null
aws elasticache describe-cache-clusters --query 'CacheClusters[].{Engine:Engine,Type:CacheNodeType,ID:CacheClusterId}' -o table 2>/dev/null
aws es list-domain-names 2>/dev/null
```

#### 2.3 Message Queues & Event Streaming

```bash
# Kafka
kubectl get pods -A -o json 2>/dev/null | jq -r '.items[] | select(.metadata.name | test("kafka|zookeeper|strimzi")) | "\(.metadata.namespace)/\(.metadata.name)"' | head -10

# AWS
aws sqs list-queues 2>/dev/null | jq '.QueueUrls | length'
aws sns list-topics 2>/dev/null | jq '.Topics | length'
aws kinesis list-streams 2>/dev/null

# RabbitMQ
kubectl get pods -A -l app=rabbitmq -o json 2>/dev/null | jq '.items | length'
```

### Phase 3: Infrastructure Discovery

#### 3.1 Compute

```bash
# Kubernetes nodes
kubectl get nodes -o json 2>/dev/null | jq '[.items[] | {name: .metadata.name, instance_type: .metadata.labels["node.kubernetes.io/instance-type"], capacity: .status.capacity}]'

# AWS EC2
aws ec2 describe-instances --query 'Reservations[].Instances[].{Type:InstanceType,State:State.Name,AZ:Placement.AvailabilityZone}' -o table 2>/dev/null

# AWS EKS
aws eks list-clusters -o json 2>/dev/null

# GCP GKE
gcloud container clusters list --format=json 2>/dev/null
```

#### 3.2 Networking

```bash
# Load balancers
kubectl get services -A --field-selector spec.type=LoadBalancer -o json 2>/dev/null | jq '[.items[] | {name: .metadata.name, ns: .metadata.namespace}]'
aws elbv2 describe-load-balancers --query 'LoadBalancers[].{Name:LoadBalancerName,Type:Type,Scheme:Scheme}' -o table 2>/dev/null

# Ingress
kubectl get ingress -A -o json 2>/dev/null | jq '[.items[] | {name: .metadata.name, ns: .metadata.namespace, hosts: [.spec.rules[].host]}]'
```

#### 3.3 Storage

```bash
# Persistent volumes
kubectl get pv -o json 2>/dev/null | jq '[.items[] | {name: .metadata.name, capacity: .spec.capacity.storage, class: .spec.storageClassName}]'

# AWS S3
aws s3api list-buckets --query 'Buckets[].Name' -o json 2>/dev/null | jq 'length'

# AWS EBS
aws ec2 describe-volumes --query 'Volumes[].{Size:Size,Type:VolumeType,State:State}' -o table 2>/dev/null | head -20
```

### Phase 4: Observability Stack Discovery

#### 4.1 Monitoring & Metrics

```bash
# Check for monitoring pods (covers Prometheus, Datadog, New Relic, Dynatrace, Splunk, Grafana, Honeycomb, Lightstep)
kubectl get pods -A -o json 2>/dev/null | jq -r '.items[] | select(.metadata.name | test("prometheus|thanos|mimir|cortex|victoriametrics|vmagent|datadog|newrelic|nri-|dynatrace|oneagent|splunk|grafana|honeycomb|lightstep")) | "\(.metadata.namespace)/\(.metadata.name)"' | head -20

# Check DaemonSets (agents often run as DaemonSets)
kubectl get daemonsets -A -o json 2>/dev/null | jq -r '.items[] | select(.metadata.name | test("datadog|newrelic|dynatrace|oneagent|splunk|fluentd|fluent-bit|node-exporter|filebeat")) | "\(.metadata.namespace)/\(.metadata.name)"'

# Helm-based detection (catches operator-managed stacks)
helm list -A -o json 2>/dev/null | jq -r '.[] | select(.chart | test("prometheus|datadog|newrelic|dynatrace|grafana|loki|tempo|splunk|elastic|victoria")) | "\(.namespace)\t\(.name)\t\(.chart)"'

# CRD-based detection (operators)
kubectl get crd 2>/dev/null | grep -i "monitoring.coreos.com\|datadoghq.com\|newrelic.com\|dynatrace.com" | head -10

# Check for CloudWatch / Stackdriver
aws cloudwatch list-metrics --query 'Metrics | length(@)' 2>/dev/null
aws cloudwatch describe-alarms --query 'MetricAlarms | length(@)' 2>/dev/null
```

#### 4.2 Logging

```bash
# Check for logging stacks
kubectl get pods -A -o json 2>/dev/null | jq -r '.items[] | select(.metadata.name | test("fluentd|fluent-bit|logstash|loki|vector|filebeat")) | "\(.metadata.namespace)/\(.metadata.name)"' | head -10

# Check for Elasticsearch/OpenSearch
kubectl get pods -A -o json 2>/dev/null | jq -r '.items[] | select(.metadata.name | test("elasticsearch|opensearch|kibana")) | "\(.metadata.namespace)/\(.metadata.name)"' | head -10

# AWS CloudWatch Logs
aws logs describe-log-groups --query 'logGroups | length(@)' 2>/dev/null
```

#### 4.3 Tracing

```bash
# Check for tracing tools (Jaeger, Zipkin, Tempo, OpenTelemetry, Honeycomb, Lightstep)
kubectl get pods -A -o json 2>/dev/null | jq -r '.items[] | select(.metadata.name | test("jaeger|zipkin|tempo|otel|opentelemetry|honeycomb|lightstep")) | "\(.metadata.namespace)/\(.metadata.name)"' | head -10

# OpenTelemetry Collector
kubectl get pods -A -l app.kubernetes.io/name=opentelemetry-collector -o json 2>/dev/null | jq '.items | length'

# Check for OTel CRDs (operator-managed)
kubectl get crd 2>/dev/null | grep -i "opentelemetry\|instrumentation" | head -5
```

#### 4.4 Alerting & On-Call

```bash
# PagerDuty / OpsGenie / VictorOps integrations
kubectl get configmaps -A -o json 2>/dev/null | jq -r '.items[] | select(.data | tostring | test("pagerduty|opsgenie|victorops|incident")) | "\(.metadata.namespace)/\(.metadata.name)"' | head -5

# AlertManager
kubectl get pods -A -o json 2>/dev/null | jq -r '.items[] | select(.metadata.name | test("alertmanager")) | "\(.metadata.namespace)/\(.metadata.name)"' | head -5
```

### Phase 5: Scale Assessment

#### 5.1 Request Volume

```bash
# If Prometheus is available, query request rates
# Build a small script to query safely with rate limiting

# Total request rate across services (if prometheus endpoint is accessible)
kubectl port-forward -n monitoring svc/prometheus 9090:9090 &
sleep 2
curl -s 'http://localhost:9090/api/v1/query?query=sum(rate(http_requests_total[5m]))' 2>/dev/null | jq '.data.result[0].value[1]'
kill %1 2>/dev/null
```

**Important:** If port-forwarding is needed, always clean up after. Limit queries to 1 per 2 seconds.

#### 5.2 Data Volumes

```bash
# Metrics ingestion rate
curl -s 'http://localhost:9090/api/v1/query?query=sum(rate(prometheus_tsdb_head_samples_appended_total[5m]))' 2>/dev/null | jq '.data.result[0].value[1]'

# Log volume (if accessible)
# Ask user for approximate daily log volume if not discoverable

# Storage usage
kubectl get pv -o json 2>/dev/null | jq '[.items[].spec.capacity.storage] | map(rtrimstr("Gi") | tonumber) | add'
```

#### 5.3 Node & Pod Counts

```bash
kubectl get nodes --no-headers 2>/dev/null | wc -l
kubectl get pods -A --no-headers 2>/dev/null | wc -l
kubectl top nodes 2>/dev/null
```

### Phase 6: Cost Discovery

Costs are often not programmatically accessible. Use this approach:

1. **Check for cost tools:**
```bash
# Kubecost / OpenCost
kubectl get pods -A -o json 2>/dev/null | jq -r '.items[] | select(.metadata.name | test("kubecost|opencost")) | "\(.metadata.namespace)/\(.metadata.name)"'

# AWS Cost Explorer (requires permissions)
# Use portable date calculation (works on both macOS and Linux)
START_DATE=$(python3 -c "import datetime; print((datetime.date.today() - datetime.timedelta(days=30)).isoformat())" 2>/dev/null || date -d '30 days ago' +%Y-%m-%d 2>/dev/null || date -v-30d +%Y-%m-%d 2>/dev/null)
END_DATE=$(date +%Y-%m-%d)
aws ce get-cost-and-usage --time-period Start=$START_DATE,End=$END_DATE --granularity MONTHLY --metrics BlendedCost --group-by Type=DIMENSION,Key=SERVICE 2>/dev/null | jq '.ResultsByTime[0].Groups[] | {Service: .Keys[0], Cost: .Metrics.BlendedCost.Amount}' | head -40
```

2. **If costs cannot be discovered, ask the user:**
```
I couldn't access cost data programmatically. Could you share approximate monthly costs for:
- Cloud infrastructure (compute, storage, networking)?
- Observability tools (monitoring, logging, tracing)?
- Any other significant SaaS/license costs related to infrastructure?
```

### Phase 7: Pain Points Discovery

Ask the user targeted questions based on what was discovered:

```
Based on what I've found, I have a few questions about operational pain points:

1. **Alert fatigue** — How many alerts do you receive per day/week? Are most actionable?
2. **Observability gaps** — Are there services or systems with insufficient monitoring?
3. **Troubleshooting time** — How long does it typically take to identify root cause of incidents?
4. **Cost concerns** — Are observability costs growing faster than your infrastructure?
5. **Tool sprawl** — Do you find yourself switching between too many tools during incidents?
6. **Data retention** — Are you satisfied with how long you retain metrics/logs/traces?
```

### Phase 8: Generate HTML Report

After collecting all data, generate a comprehensive HTML report. The report must be:
- Self-contained (single HTML file, inline CSS, no external dependencies)
- Professional and visually clean
- Tailored to what was actually discovered (omit sections with no data)
- Opened automatically in the user's browser

Use this command to open the report:
```bash
# Save to current directory for easy access
REPORT_PATH="./discovery-report.html"

# macOS
open "$REPORT_PATH"

# Linux
xdg-open "$REPORT_PATH"

# WSL
wslview "$REPORT_PATH"

# Fallback
echo "Report saved to: $(pwd)/discovery-report.html"
```

## HTML Report Structure

Generate the report using the template structure below. Adapt sections based on what was discovered — omit empty sections, expand sections with rich data.

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Infrastructure & Observability Discovery Report</title>
<style>
  :root {
    --primary: #1a1a2e;
    --accent: #4f46e5;
    --accent-light: #818cf8;
    --bg: #f8fafc;
    --card: #ffffff;
    --text: #1e293b;
    --text-muted: #64748b;
    --border: #e2e8f0;
    --success: #10b981;
    --warning: #f59e0b;
    --danger: #ef4444;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
  }
  .header {
    background: var(--primary);
    color: white;
    padding: 3rem 2rem;
    text-align: center;
  }
  .header h1 { font-size: 2rem; margin-bottom: 0.5rem; }
  .header p { color: #94a3b8; font-size: 1.1rem; }
  .container { max-width: 1200px; margin: 0 auto; padding: 2rem; }
  .summary-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 1rem;
    margin: 2rem 0;
  }
  .summary-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.5rem;
    text-align: center;
  }
  .summary-card .value {
    font-size: 2rem;
    font-weight: 700;
    color: var(--accent);
  }
  .summary-card .label {
    color: var(--text-muted);
    font-size: 0.875rem;
    margin-top: 0.25rem;
  }
  .section {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 2rem;
    margin: 1.5rem 0;
  }
  .section h2 {
    font-size: 1.4rem;
    margin-bottom: 1rem;
    color: var(--primary);
    border-bottom: 2px solid var(--accent);
    padding-bottom: 0.5rem;
  }
  .section h3 {
    font-size: 1.1rem;
    margin: 1.5rem 0 0.75rem;
    color: var(--text);
  }
  table {
    width: 100%;
    border-collapse: collapse;
    margin: 1rem 0;
    font-size: 0.9rem;
  }
  th, td {
    padding: 0.75rem 1rem;
    text-align: left;
    border-bottom: 1px solid var(--border);
  }
  th {
    background: var(--bg);
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    font-size: 0.75rem;
    letter-spacing: 0.05em;
  }
  .tag {
    display: inline-block;
    padding: 0.25rem 0.75rem;
    border-radius: 9999px;
    font-size: 0.8rem;
    font-weight: 500;
    margin: 0.125rem;
  }
  .tag-blue { background: #dbeafe; color: #1d4ed8; }
  .tag-green { background: #d1fae5; color: #065f46; }
  .tag-purple { background: #ede9fe; color: #5b21b6; }
  .tag-orange { background: #ffedd5; color: #9a3412; }
  .tag-red { background: #fee2e2; color: #991b1b; }
  .tag-gray { background: #f1f5f9; color: #475569; }
  .pain-point {
    border-left: 4px solid var(--warning);
    padding: 1rem 1.5rem;
    margin: 0.75rem 0;
    background: #fffbeb;
    border-radius: 0 8px 8px 0;
  }
  .recommendation {
    border-left: 4px solid var(--success);
    padding: 1rem 1.5rem;
    margin: 0.75rem 0;
    background: #ecfdf5;
    border-radius: 0 8px 8px 0;
  }
  .env-badge {
    display: inline-block;
    padding: 0.2rem 0.6rem;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
  }
  .env-prod { background: #fee2e2; color: #991b1b; }
  .env-staging { background: #fef3c7; color: #92400e; }
  .env-dev { background: #d1fae5; color: #065f46; }
  .footer {
    text-align: center;
    padding: 2rem;
    color: var(--text-muted);
    font-size: 0.85rem;
  }
  .footer a { color: var(--accent); text-decoration: none; }
  ul { padding-left: 1.5rem; margin: 0.5rem 0; }
  li { margin: 0.25rem 0; }
</style>
</head>
<body>
<div class="header">
  <h1>Infrastructure & Observability Discovery Report</h1>
  <p>Generated on {{DATE}} for {{COMPANY/PROJECT}}</p>
</div>
<div class="container">

  <!-- Executive Summary Cards -->
  <div class="summary-grid">
    <div class="summary-card">
      <div class="value">{{N}}</div>
      <div class="label">Environments</div>
    </div>
    <div class="summary-card">
      <div class="value">{{N}}</div>
      <div class="label">Services</div>
    </div>
    <div class="summary-card">
      <div class="value">{{N}}</div>
      <div class="label">K8s Nodes</div>
    </div>
    <div class="summary-card">
      <div class="value">{{TOOL}}</div>
      <div class="label">Primary Monitoring</div>
    </div>
  </div>

  <!-- Section: Environments -->
  <div class="section">
    <h2>Environments</h2>
    <!-- Table of environments with cloud provider, region, cluster info -->
  </div>

  <!-- Section: Tech Stack -->
  <div class="section">
    <h2>Tech Stack</h2>
    <h3>Languages & Frameworks</h3>
    <!-- Tags for each language/framework -->
    <h3>Databases & Storage</h3>
    <!-- Table of databases -->
    <h3>Message Queues & Event Streaming</h3>
    <!-- Table of queues -->
  </div>

  <!-- Section: Infrastructure -->
  <div class="section">
    <h2>Infrastructure</h2>
    <h3>Compute</h3>
    <!-- Node types, counts, capacity -->
    <h3>Networking</h3>
    <!-- Load balancers, ingress -->
    <h3>Storage</h3>
    <!-- PVs, S3 buckets, etc -->
  </div>

  <!-- Section: Observability Stack -->
  <div class="section">
    <h2>Observability Stack</h2>
    <h3>Metrics & Monitoring</h3>
    <!-- Tools, configuration -->
    <h3>Logging</h3>
    <!-- Log pipeline -->
    <h3>Tracing</h3>
    <!-- Tracing tools -->
    <h3>Alerting & On-Call</h3>
    <!-- Alert routing -->
  </div>

  <!-- Section: Scale -->
  <div class="section">
    <h2>Scale</h2>
    <!-- Request rates, data volumes, pod counts -->
  </div>

  <!-- Section: Costs -->
  <div class="section">
    <h2>Costs</h2>
    <!-- Monthly costs breakdown -->
  </div>

  <!-- Section: Pain Points & Recommendations -->
  <div class="section">
    <h2>Pain Points & Recommendations</h2>
    <!-- Pain points with recommendation callouts -->
  </div>

</div>
<div class="footer">
  <p>Generated by <a href="https://oodle.ai">Oodle</a> Discovery Agent</p>
</div>
</body>
</html>
```

## Report Generation Rules

1. **Replace all `{{PLACEHOLDER}}` values** with actual discovered data.
2. **Omit sections** where no data was found and the user did not provide information.
3. **Use tags** (`.tag-blue`, `.tag-green`, etc.) for languages, frameworks, and tools.
4. **Use environment badges** (`.env-prod`, `.env-staging`, `.env-dev`) when listing per-environment data.
5. **Use pain-point callouts** for issues discovered or reported by the user.
6. **Use recommendation callouts** for actionable suggestions based on findings.
7. **Include actual numbers** — node counts, pod counts, request rates, costs — not placeholders.
8. **Save the file** to a discoverable location and open it in the browser.

## Rate Limiting & Safety

### Throttling Rules

| Target System | Max Rate | Backoff |
|---------------|----------|---------|
| Kubernetes API | 5 req/sec | 2s on 429 |
| AWS API | 2 req/sec | 5s on throttle |
| GCP API | 2 req/sec | 5s on throttle |
| Prometheus/metrics endpoint | 1 req/2sec | 5s on timeout |
| Any port-forward | 1 at a time | Clean up after use |

### Safety Rules

- **Never** run `kubectl delete`, `kubectl apply`, `kubectl patch`, or any write operation.
- **Never** run `aws` commands that create, modify, or delete resources.
- **Never** modify files in the user's workspace (except the output report).
- **Always** clean up port-forwards after use (`kill %1` or track PIDs).
- **Always** use `--no-headers` or `-o json` to avoid interactive prompts.
- **If a command hangs** for more than 10 seconds, kill it and move on.
- **If a command requires credentials** you don't have, skip it and note the gap.

## Failure Handling

| Situation | Action |
|-----------|--------|
| CLI tool not installed | Note it as unavailable, skip related checks |
| No credentials/permissions | Ask user if they can provide access, otherwise skip |
| Command times out | Kill after 10s, note as "unable to assess" |
| Empty results | Try alternative discovery method, then ask user |
| Rate limited (429) | Wait 10s, retry once, then skip |
| Multiple clusters/accounts | Discover each separately, label by environment |

## Asking Clarifying Questions

When information cannot be discovered programmatically, ask the user. Group questions together rather than asking one at a time. Example:

```
I've completed the automated discovery. I have a few questions to fill in gaps:

1. What is your approximate monthly cloud spend?
2. Which observability vendor(s) do you pay for (e.g., Datadog, New Relic, Splunk)?
3. What's your approximate monthly observability spend?
4. How many engineers are on-call for production?
5. What's your biggest operational pain point right now?
```

## References

- [Oodle](https://oodle.ai) — Observability platform
- [kubectl cheat sheet](https://kubernetes.io/docs/reference/kubectl/cheatsheet/)
- [AWS CLI reference](https://docs.aws.amazon.com/cli/latest/reference/)
