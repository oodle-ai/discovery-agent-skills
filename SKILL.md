---
name: oodle-discovery
description: Discover a company's tech stack, observability stack, infrastructure scale, costs, and pain points across all environments. Produces a focused executive-level HTML report with bird's-eye view of environments, observability scale numbers, and costs.
metadata:
  version: "1.0.0"
  author: oodle-ai
  repository: https://github.com/oodle-ai/discovery-agent-skills
  tags: oodle,discovery,observability,tech-stack,infrastructure,assessment
  globs: ""
  alwaysApply: "false"
---

# Infrastructure & Observability Discovery

This skill guides the agent through a systematic discovery of a company's tech stack, observability stack, infrastructure scale, costs, and operational pain points. The output is a focused, executive-level HTML report — a bird's-eye view suitable for a buyer or tech champion who needs to quickly understand environments, scale, and costs without operational-level detail.

For install instructions, see [README.md](README.md).

## Principles

1. **Non-destructive only.** Never modify, delete, or write to any system. All operations are read-only.
2. **Rate-limited.** Wait 1-2 seconds between API calls. Never issue more than 5 requests per second to any single endpoint.
3. **Ask when uncertain.** If information cannot be discovered programmatically, ask the user.
4. **Plan first.** Always present the discovery plan and get user approval before executing.
5. **Multi-environment aware.** Discover all environments (dev, staging, prod, etc.) separately.
6. **Executive-level output.** The report is for a buyer/tech champion. Show aggregate numbers, environment comparisons, and scale — not per-pod or per-node details. Approximations are better than no data.
7. **Observability scale is critical.** Always measure and report telemetry volumes (metrics samples/sec, log GB/day, trace spans/sec) alongside tool names.

## Execution Flow

### Phase 0: Present Plan

Before doing anything, present this plan to the user and ask for approval:

```
I'll discover your infrastructure and observability setup. Here's my plan:

1. **Environment Detection** — Identify all environments (cloud accounts, k8s clusters, regions)
2. **Tech Stack Discovery** — Languages, frameworks, databases, message queues, caches
3. **Infrastructure Discovery** — Cloud provider, compute, networking, storage
4. **Observability Stack Discovery** — Monitoring, logging, tracing, alerting tools
5. **Scale Assessment** — Infra scale per environment + observability scale (metrics ingestion rate, log volume, trace throughput, active time series)
6. **Cost Discovery** — Cloud spend, observability tool costs, license costs
7. **Pain Points** — Observability-specific: alert fatigue, gaps in coverage, tool sprawl, cost, correlation issues

I will only perform read-only operations. No changes will be made to your systems.
The output will be a focused executive summary — bird's-eye view of your setup, not operational-level detail.
Shall I proceed?
```

Wait for user confirmation before continuing.

### Phase 1: Environment Detection

Discover all environments by checking these sources in order. Stop each check after 5 seconds if no response.

**Before starting Phase 1, cache commonly used Kubernetes data to avoid redundant API calls:**

```bash
# Cache pod list (used across many phases)
kubectl get pods -A -o json 2>/dev/null > /tmp/discovery-pods.json

# Cache CRD list (used across multiple phases)
kubectl get crd 2>/dev/null > /tmp/discovery-crds.txt
```

Use `/tmp/discovery-pods.json` and `/tmp/discovery-crds.txt` for all subsequent pod and CRD queries instead of re-fetching from the API.

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

# CRDs (from cached data)
grep -i "monitoring\|observability\|prometheus\|datadog\|elastic\|cert-manager\|istio\|linkerd" /tmp/discovery-crds.txt | head -20
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
# Kafka (from cached pod data)
jq -r '.items[] | select(.metadata.name | test("kafka|zookeeper|strimzi")) | "\(.metadata.namespace)/\(.metadata.name)"' /tmp/discovery-pods.json | head -10

# AWS
aws sqs list-queues 2>/dev/null | jq '.QueueUrls | length'
aws sns list-topics 2>/dev/null | jq '.Topics | length'
aws kinesis list-streams 2>/dev/null

# RabbitMQ (from cached pod data)
jq '[.items[] | select(.metadata.labels.app == "rabbitmq")] | length' /tmp/discovery-pods.json
```

### Phase 3: Infrastructure Discovery

**Note:** Collect per-node and per-instance data here for internal analysis, but only report **aggregates** in the final HTML report (total nodes, total vCPU, total memory, instance type families). Do NOT list individual node IPs or per-node utilization in the report.

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
# Check for monitoring pods (from cached pod data)
jq -r '.items[] | select(.metadata.name | test("prometheus|thanos|mimir|cortex|victoriametrics|vmagent|datadog|newrelic|nri-|dynatrace|oneagent|splunk|grafana|honeycomb|lightstep")) | "\(.metadata.namespace)/\(.metadata.name)"' /tmp/discovery-pods.json | head -20

# Check DaemonSets (agents often run as DaemonSets)
kubectl get daemonsets -A -o json 2>/dev/null | jq -r '.items[] | select(.metadata.name | test("datadog|newrelic|dynatrace|oneagent|splunk|fluentd|fluent-bit|node-exporter|filebeat")) | "\(.metadata.namespace)/\(.metadata.name)"'

# Helm-based detection (catches operator-managed stacks)
helm list -A -o json 2>/dev/null | jq -r '.[] | select(.chart | test("prometheus|datadog|newrelic|dynatrace|grafana|loki|tempo|splunk|elastic|victoria")) | "\(.namespace)\t\(.name)\t\(.chart)"'

# CRD-based detection (from cached data)
grep -i "monitoring.coreos.com\|datadoghq.com\|newrelic.com\|dynatrace.com" /tmp/discovery-crds.txt | head -10

# Check for CloudWatch / Stackdriver
aws cloudwatch list-metrics --query 'Metrics | length(@)' 2>/dev/null
aws cloudwatch describe-alarms --query 'MetricAlarms | length(@)' 2>/dev/null
```

#### 4.2 Logging

```bash
# Check for logging stacks (from cached pod data)
jq -r '.items[] | select(.metadata.name | test("fluentd|fluent-bit|logstash|loki|vector|filebeat")) | "\(.metadata.namespace)/\(.metadata.name)"' /tmp/discovery-pods.json | head -10

# Check for Elasticsearch/OpenSearch (from cached pod data)
jq -r '.items[] | select(.metadata.name | test("elasticsearch|opensearch|kibana")) | "\(.metadata.namespace)/\(.metadata.name)"' /tmp/discovery-pods.json | head -10

# AWS CloudWatch Logs
aws logs describe-log-groups --query 'logGroups | length(@)' 2>/dev/null
```

#### 4.3 Tracing

```bash
# Check for tracing tools (from cached pod data)
jq -r '.items[] | select(.metadata.name | test("jaeger|zipkin|tempo|otel|opentelemetry|honeycomb|lightstep")) | "\(.metadata.namespace)/\(.metadata.name)"' /tmp/discovery-pods.json | head -10

# OpenTelemetry Collector (from cached pod data)
jq '[.items[] | select(.metadata.labels["app.kubernetes.io/name"] == "opentelemetry-collector")] | length' /tmp/discovery-pods.json

# Check for OTel CRDs (from cached data)
grep -i "opentelemetry\|instrumentation" /tmp/discovery-crds.txt | head -5
```

#### 4.4 Alerting & On-Call

```bash
# PagerDuty / OpsGenie / VictorOps integrations
kubectl get configmaps -A -o json 2>/dev/null | jq -r '.items[] | select(.data | tostring | test("pagerduty|opsgenie|victorops|incident")) | "\(.metadata.namespace)/\(.metadata.name)"' | head -5

# AlertManager (from cached pod data)
jq -r '.items[] | select(.metadata.name | test("alertmanager")) | "\(.metadata.namespace)/\(.metadata.name)"' /tmp/discovery-pods.json | head -5
```

### Phase 5: Scale Assessment

**Goal:** Produce high-level scale numbers suitable for an executive summary. Focus on aggregate totals across environments, not per-pod or per-node breakdowns.

#### 5.1 Infrastructure Scale (per environment)

```bash
# For each cluster/context, collect aggregate numbers only
kubectl get nodes --no-headers 2>/dev/null | wc -l
kubectl get pods -A --no-headers 2>/dev/null | wc -l
kubectl get namespaces --no-headers 2>/dev/null | wc -l

# Total compute capacity (aggregate)
kubectl get nodes -o json 2>/dev/null | jq '{
  total_nodes: (.items | length),
  total_vcpu: ([.items[].status.capacity.cpu | tonumber] | add),
  total_memory_gi: ([.items[].status.capacity.memory | rtrimstr("Ki") | tonumber / 1048576] | add | floor),
  instance_types: [.items[].metadata.labels["node.kubernetes.io/instance-type"]] | unique
}'

# Average utilization (single summary line)
kubectl top nodes --no-headers 2>/dev/null | awk '{cpu+=$3; mem+=$5; n++} END {printf "Avg CPU: %d%%, Avg Memory: %d%%\n", cpu/n, mem/n}'
```

#### 5.2 Observability Volume Estimation

This is the most critical section. The goal is to produce concrete volume numbers:
- **Active time series** (total unique metric series)
- **Metrics ingestion rate** (samples/sec)
- **Log volume** (GB/day)
- **Trace volume** (GB/day or spans/sec)

**Strategy:** Users typically have MCP servers or CLI tools connected to their observability stack (e.g., Datadog CLI, Grafana Cloud CLI, New Relic CLI, or custom MCP integrations). Use these as the **primary** method to query volume data. Query patterns over the **last 7 days** to get a representative average — not just a point-in-time snapshot.

**Step 1: Identify available observability query tools**

Check what MCP servers, CLIs, or APIs the user has available:
```
I need to query your observability stack for volume numbers. Do you have any of the following available?

- MCP server connected to your metrics/logs/traces backend (e.g., Datadog MCP, Grafana MCP, custom MCP)
- CLI tools (e.g., `datadog`, `grafana-cli`, `newrelic`, `logcli`, `promtool`)
- Direct API access to your observability backend (Prometheus, Grafana Cloud, Datadog, etc.)

Which tools or integrations can I use to query telemetry volume?
```

**Step 2: Query volume over last 7 days via MCP/CLI (preferred)**

Use whatever tool the user has available. The key queries to run:

```
# These are example queries — adapt to the user's specific tool/API:

# Active time series (7-day average)
# Prometheus/Thanos/Mimir: avg_over_time(prometheus_tsdb_head_series[7d])
# Datadog: metrics.list with count
# Grafana Cloud: /api/v1/query?query=sum(scrape_samples_scraped)

# Metrics ingestion rate (7-day average samples/sec)
# Prometheus: avg_over_time(rate(prometheus_tsdb_head_samples_appended_total[1h])[7d:1h])
# VictoriaMetrics: avg_over_time(rate(vm_rows_inserted_total[1h])[7d:1h])

# Log volume (average GB/day over last 7 days)
# Loki: sum(bytes_over_time({job=~".+"}[7d])) / 7 / 1e9
# Elasticsearch: _cat/indices?v&s=store.size:desc (sum store.size, divide by retention days)
# Datadog: logs estimated usage API
# CloudWatch: GetMetricData for IncomingBytes

# Trace volume (average GB/day or spans/sec over last 7 days)
# Tempo: tempo_ingester_bytes_received_total rate over 7d
# Jaeger: jaeger_collector_spans_received_total rate over 7d
# Datadog: trace estimated usage API
```

**Step 3: Fallback — query via port-forward to in-cluster metrics**

If no MCP/CLI is available, fall back to direct Prometheus-compatible queries:

```bash
# Detect which metrics endpoint is available
METRICS_SVC=$(kubectl get svc -A -o json 2>/dev/null | jq -r '.items[] | select(.metadata.name | test("prometheus|vmagent|vmselect|thanos-query|mimir-query")) | "\(.metadata.namespace)/\(.metadata.name):\(.spec.ports[0].port)"' | head -1)
if [ -n "$METRICS_SVC" ]; then
  NS=$(echo $METRICS_SVC | cut -d/ -f1)
  SVC_PORT=$(echo $METRICS_SVC | cut -d/ -f2)
  kubectl port-forward -n $NS svc/${SVC_PORT%:*} 9090:${SVC_PORT#*:} &
  PF_PID=$!
  sleep 3

  # Active time series (current)
  curl -s 'http://localhost:9090/api/v1/query?query=prometheus_tsdb_head_series' 2>/dev/null | jq '.data.result[0].value[1]'

  # Metrics ingestion rate — 7-day average (samples/sec)
  curl -s 'http://localhost:9090/api/v1/query?query=avg_over_time(rate(prometheus_tsdb_head_samples_appended_total[1h])[7d:1h])' 2>/dev/null | jq '.data.result[0].value[1]'
  # Alternative for VictoriaMetrics
  curl -s 'http://localhost:9090/api/v1/query?query=avg_over_time(rate(vm_rows_inserted_total[1h])[7d:1h])' 2>/dev/null | jq '.data.result[0].value[1]'

  # Scrape targets count
  curl -s 'http://localhost:9090/api/v1/query?query=count(up)' 2>/dev/null | jq '.data.result[0].value[1]'

  # Log volume if Loki metrics are exposed via Prometheus
  curl -s 'http://localhost:9090/api/v1/query?query=sum(rate(loki_distributor_bytes_received_total[1h]))*86400/1e9' 2>/dev/null | jq '.data.result[0].value[1]'

  # Trace ingestion rate if Tempo/OTel metrics are exposed
  curl -s 'http://localhost:9090/api/v1/query?query=sum(rate(tempo_distributor_bytes_received_total[1h]))*86400/1e9' 2>/dev/null | jq '.data.result[0].value[1]'
  curl -s 'http://localhost:9090/api/v1/query?query=sum(rate(otelcol_receiver_accepted_spans_total[1h]))' 2>/dev/null | jq '.data.result[0].value[1]'

  kill $PF_PID 2>/dev/null
fi
```

**Step 4: Estimate from resource allocations (last resort)**

If neither MCP/CLI nor direct queries are available, estimate from infrastructure:

```bash
# Log pipeline pods — resource requests are a rough proxy for throughput
jq -r '.items[] | select(.metadata.name | test("log-receiver|vector|fluentd|fluent-bit|logstash|loki-write")) | "\(.metadata.namespace)/\(.metadata.name) cpu:\(.spec.containers[0].resources.requests.cpu // "unknown") mem:\(.spec.containers[0].resources.requests.memory // "unknown")"' /tmp/discovery-pods.json | head -10

# Trace collector pods
jq -r '.items[] | select(.metadata.name | test("otel|opentelemetry|jaeger|tempo")) | "\(.metadata.namespace)/\(.metadata.name) cpu:\(.spec.containers[0].resources.requests.cpu // "unknown") mem:\(.spec.containers[0].resources.requests.memory // "unknown")"' /tmp/discovery-pods.json | head -10

# Observability storage (PVCs)
kubectl get pvc -A -o json 2>/dev/null | jq '[.items[] | select(.metadata.name | test("prometheus|thanos|mimir|loki|tempo|elastic|opensearch|clickhouse|vector|grafana|victoria")) | .spec.resources.requests.storage | rtrimstr("Gi") | tonumber] | {total_obs_storage_gi: add, count: length}'

# S3 buckets related to observability (if AWS access available)
aws s3api list-buckets --query 'Buckets[].Name' -o json 2>/dev/null | jq '[.[] | select(test("metric|log|trace|thanos|loki|tempo|observ"))]'
```

**Important:** Always report the numbers you find, even if approximate. Use `~` prefix for estimates (e.g., "~50K samples/sec", "~150 GB/day logs"). Approximate numbers are far more useful than no numbers.

#### 5.3 Application Scale

```bash
# Total request rate across services (if metrics endpoint available)
# Set up a fresh port-forward if needed
METRICS_SVC=$(kubectl get svc -A -o json 2>/dev/null | jq -r '.items[] | select(.metadata.name | test("prometheus|vmagent|vmselect|thanos-query|mimir-query")) | "\(.metadata.namespace)/\(.metadata.name):\(.spec.ports[0].port)"' | head -1)
if [ -n "$METRICS_SVC" ]; then
  NS=$(echo $METRICS_SVC | cut -d/ -f1)
  SVC_PORT=$(echo $METRICS_SVC | cut -d/ -f2)
  kubectl port-forward -n $NS svc/${SVC_PORT%:*} 9090:${SVC_PORT#*:} &
  PF_PID=$!
  sleep 3
  curl -s 'http://localhost:9090/api/v1/query?query=sum(rate(http_requests_total[5m]))' 2>/dev/null | jq '.data.result[0].value[1]'
  kill $PF_PID 2>/dev/null
fi

# Helm releases count (proxy for service count)
helm list -A --no-headers 2>/dev/null | wc -l

# Deployments count
kubectl get deployments -A --no-headers 2>/dev/null | wc -l
```

#### 5.4 If scale data is not discoverable programmatically

Ask the user:
```
I need a few numbers to complete the scale picture. Even rough approximations based on your last 7 days are helpful:

1. **Active time series** — How many unique metric series does your system track?
2. **Metrics ingestion** — Approximate samples/sec ingested?
3. **Log volume** — Approximate GB/day of logs ingested (average over last week)?
4. **Trace volume** — Approximate GB/day or spans/sec (average over last week)?
5. **Request rate** — Approximate requests/sec across all services?
6. **Data retention** — How long do you retain metrics / logs / traces?
```

### Phase 6: Cost Discovery

Costs are often not programmatically accessible. Use this approach:

1. **Check for cost tools:**
```bash
# Kubecost / OpenCost (from cached pod data)
jq -r '.items[] | select(.metadata.name | test("kubecost|opencost")) | "\(.metadata.namespace)/\(.metadata.name)"' /tmp/discovery-pods.json

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

Ask the user targeted questions about **observability-specific** pain points only. Do NOT ask about general infrastructure, deployment, or application-level pain points.

```
Based on what I've found, I have a few questions about observability pain points:

1. **Alert fatigue** — How many alerts do you receive per day/week? Are most actionable?
2. **Observability gaps** — Are there services or systems with insufficient monitoring, logging, or tracing?
3. **Troubleshooting time** — How long does it typically take to identify root cause of incidents? Do you have the right signals?
4. **Observability cost** — Are observability costs growing faster than your infrastructure? Any vendor lock-in concerns?
5. **Tool sprawl** — Do you find yourself switching between too many observability tools during incidents?
6. **Data retention** — Are you satisfied with how long you retain metrics/logs/traces? Any compliance requirements unmet?
7. **Correlation gaps** — Can you easily correlate metrics, logs, and traces for a single request?
```

### Phase 8: Generate HTML Report

After collecting all data, generate a focused executive-level HTML report. The report must be:
- Self-contained (single HTML file, inline CSS, no external dependencies)
- Professional and visually clean
- **Concise** — a bird's-eye view, not an operational runbook. Scannable in under 2 minutes.
- Tailored to what was actually discovered (omit sections with no data)
- Opened automatically in the user's browser

**Do NOT include:** per-pod tables, per-node resource breakdowns, ASCII architecture diagrams, service replica counts, individual PV listings, or any detail that belongs in an operational dashboard rather than an executive summary.

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

The report is designed for a **buyer or tech champion** who needs a bird's-eye view of their environment. It should be scannable in under 2 minutes.

**Report philosophy:**
- Lead with aggregate numbers and environment-level comparisons
- Show scale in human-readable terms (e.g., "~50K samples/sec", "~200 GB/day logs")
- Approximate numbers are more useful than no numbers
- Omit per-pod, per-node, per-service breakdowns — those belong in operational dashboards, not a discovery report
- Focus on: environments, tech stack summary, observability scale, costs, and pain points
- Each section should fit on one screen without scrolling

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
    --bg: #f8fafc;
    --card: #ffffff;
    --text: #1e293b;
    --text-muted: #64748b;
    --border: #e2e8f0;
    --success: #10b981;
    --warning: #f59e0b;
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
  .container { max-width: 1100px; margin: 0 auto; padding: 2rem; }
  .summary-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 1rem;
    margin: 2rem 0;
  }
  .summary-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.25rem;
    text-align: center;
  }
  .summary-card .value {
    font-size: 1.75rem;
    font-weight: 700;
    color: var(--accent);
  }
  .summary-card .label {
    color: var(--text-muted);
    font-size: 0.8rem;
    margin-top: 0.25rem;
  }
  .section {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.75rem;
    margin: 1.25rem 0;
  }
  .section h2 {
    font-size: 1.3rem;
    margin-bottom: 0.75rem;
    color: var(--primary);
    border-bottom: 2px solid var(--accent);
    padding-bottom: 0.5rem;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    margin: 0.75rem 0;
    font-size: 0.9rem;
  }
  th, td {
    padding: 0.6rem 0.75rem;
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
    padding: 0.2rem 0.6rem;
    border-radius: 9999px;
    font-size: 0.75rem;
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
    padding: 0.75rem 1.25rem;
    margin: 0.5rem 0;
    background: #fffbeb;
    border-radius: 0 8px 8px 0;
  }
  .recommendation {
    border-left: 4px solid var(--success);
    padding: 0.75rem 1.25rem;
    margin: 0.5rem 0;
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

  <!-- Executive Summary — the most important section. Key numbers at a glance. -->
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
      <div class="value">{{N}} nodes</div>
      <div class="label">Compute (total)</div>
    </div>
    <div class="summary-card">
      <div class="value">~{{N}}/sec</div>
      <div class="label">Metrics Ingestion</div>
    </div>
    <div class="summary-card">
      <div class="value">~{{N}} GB/day</div>
      <div class="label">Log Volume</div>
    </div>
    <div class="summary-card">
      <div class="value">~{{N}}/sec</div>
      <div class="label">Trace Spans</div>
    </div>
    <div class="summary-card">
      <div class="value">${{N}}/mo</div>
      <div class="label">Est. Cloud Spend</div>
    </div>
    <div class="summary-card">
      <div class="value">{{N}}</div>
      <div class="label">Team Size</div>
    </div>
  </div>

  <!-- Section: Environments — one row per environment, keep it compact -->
  <div class="section">
    <h2>Environments</h2>
    <table>
      <thead>
        <tr><th>Environment</th><th>Cloud / Region</th><th>Cluster</th><th>Nodes</th><th>Services</th></tr>
      </thead>
      <tbody>
        <!-- One row per environment. Show aggregate node/service count per env. -->
        <tr>
          <td><span class="env-badge env-prod">PROD</span></td>
          <td>{{cloud}} {{region}}</td>
          <td>{{cluster_name}}</td>
          <td>{{N}}</td>
          <td>{{N}}</td>
        </tr>
        <!-- Repeat for each environment -->
      </tbody>
    </table>
  </div>

  <!-- Section: Tech Stack — tags only, no per-service tables -->
  <div class="section">
    <h2>Tech Stack</h2>
    <p><strong>Languages:</strong>
      <span class="tag tag-blue">{{lang}}</span>
      <!-- tags for each language -->
    </p>
    <p style="margin-top: 0.5rem;"><strong>Databases:</strong>
      <span class="tag tag-green">{{db}}</span>
      <!-- tags for each database -->
    </p>
    <p style="margin-top: 0.5rem;"><strong>Infra & IaC:</strong>
      <span class="tag tag-purple">{{tool}}</span>
      <!-- tags for IaC, CI/CD, orchestration -->
    </p>
  </div>

  <!-- Section: Observability Stack — what tools, NOT per-pod details -->
  <div class="section">
    <h2>Observability Stack</h2>
    <table>
      <thead>
        <tr><th>Signal</th><th>Tools</th><th>Scale</th></tr>
      </thead>
      <tbody>
        <tr>
          <td><strong>Metrics</strong></td>
          <td>{{tools as tags or comma-separated}}</td>
          <td>~{{N}} samples/sec, {{N}} active series</td>
        </tr>
        <tr>
          <td><strong>Logs</strong></td>
          <td>{{tools}}</td>
          <td>~{{N}} GB/day</td>
        </tr>
        <tr>
          <td><strong>Traces</strong></td>
          <td>{{tools}}</td>
          <td>~{{N}} spans/sec</td>
        </tr>
        <tr>
          <td><strong>Alerting</strong></td>
          <td>{{tools}}</td>
          <td>{{N}} alert rules</td>
        </tr>
      </tbody>
    </table>
    <p style="margin-top: 0.75rem; font-size: 0.85rem; color: var(--text-muted);">
      Observability storage: ~{{N}} GB total across all signals. Retention: {{metrics_retention}} / {{logs_retention}} / {{traces_retention}}.
    </p>
  </div>

  <!-- Section: Scale at a Glance — environment comparison, NOT per-node -->
  <div class="section">
    <h2>Scale at a Glance</h2>
    <table>
      <thead>
        <tr><th>Metric</th><th>Dev</th><th>Staging</th><th>Prod</th></tr>
      </thead>
      <tbody>
        <tr><td>Nodes</td><td>{{N}}</td><td>{{N}}</td><td>{{N}}</td></tr>
        <tr><td>Total vCPU</td><td>{{N}}</td><td>{{N}}</td><td>{{N}}</td></tr>
        <tr><td>Total Memory</td><td>{{N}} GB</td><td>{{N}} GB</td><td>{{N}} GB</td></tr>
        <tr><td>Pods / Services</td><td>{{N}}</td><td>{{N}}</td><td>{{N}}</td></tr>
        <tr><td>Avg CPU Util</td><td>{{N}}%</td><td>{{N}}%</td><td>{{N}}%</td></tr>
        <tr><td>Persistent Storage</td><td>{{N}} GB</td><td>{{N}} GB</td><td>{{N}} GB</td></tr>
      </tbody>
    </table>
  </div>

  <!-- Section: Costs — keep it to a summary table -->
  <div class="section">
    <h2>Costs (estimated monthly)</h2>
    <table>
      <thead>
        <tr><th>Category</th><th>Estimated Spend</th><th>Notes</th></tr>
      </thead>
      <tbody>
        <tr><td>Compute (EKS/EC2)</td><td>${{N}}</td><td>{{instance types, regions}}</td></tr>
        <tr><td>Storage (S3/EBS/PV)</td><td>${{N}}</td><td></td></tr>
        <tr><td>Observability Tools</td><td>${{N}}</td><td>{{vendor names}}</td></tr>
        <tr><td>Managed Databases</td><td>${{N}}</td><td>{{db names}}</td></tr>
        <tr><td><strong>Total</strong></td><td><strong>${{N}}</strong></td><td></td></tr>
      </tbody>
    </table>
  </div>

  <!-- Section: Observability Pain Points — brief, actionable, observability-only -->
  <div class="section">
    <h2>Observability Pain Points</h2>
    <div class="pain-point">
      <strong>{{Pain point title}}</strong>
      <p>{{1-2 sentence description with supporting data}}</p>
    </div>
    <div class="recommendation">
      <strong>Recommendation:</strong> {{1-2 sentence actionable suggestion}}
    </div>
    <!-- Repeat for each pain point. Limit to top 3-5 most impactful. Only include observability-related pain points (alerting, monitoring gaps, cost, tool sprawl, correlation, retention). -->
  </div>

</div>
<div class="footer">
  <p>Generated by <a href="https://oodle.ai">Oodle</a> Discovery Agent</p>
</div>
</body>
</html>
```

## Report Generation Rules

1. **Executive-first.** The report is for a buyer or tech champion who needs a bird's-eye view. Lead with the numbers that matter: environments, scale, costs, pain points. Do NOT include per-pod, per-node, or per-service breakdowns.
2. **Replace all `{{PLACEHOLDER}}` values** with actual discovered data. Use approximations (prefixed with ~) when exact numbers are unavailable.
3. **Omit sections** where no data was found and the user did not provide information.
4. **Keep it scannable.** Each section should fit on one screen. Use tables with 3-6 rows, not 20+. Use tags for tech stack, not detailed tables.
5. **Observability scale is mandatory.** The Observability Stack section MUST include scale numbers (samples/sec, GB/day, spans/sec) alongside tool names. If exact numbers are unavailable, estimate from resource allocations or ask the user.
6. **Environment comparison.** The Scale section should compare environments side-by-side (dev vs staging vs prod) in a single table, not describe each in isolation.
7. **Use tags** (`.tag-blue`, `.tag-green`, etc.) for languages, frameworks, and tools — not detailed tables listing every component.
8. **Use environment badges** (`.env-prod`, `.env-staging`, `.env-dev`) in the environments table.
9. **Pain points: top 3-5 only, observability-focused.** Only include pain points related to observability (alerting, monitoring gaps, log/trace coverage, cost, tool sprawl, correlation, retention). Do NOT include general infrastructure, deployment, or application-level pain points. Each pain point gets 1-2 sentences max, followed by a 1-2 sentence recommendation.
10. **No architecture diagrams.** ASCII diagrams add clutter. The tool names and scale numbers tell the story.
11. **Include actual numbers** — node counts, ingestion rates, costs — not placeholders. Approximations are fine and encouraged.
12. **Save the file** to a discoverable location and open it in the browser.

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
- **Always** clean up port-forwards after use (track PIDs with `$!` and `kill $PID`).
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
