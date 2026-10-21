# Infra Diagrams — Design (PoC)

Date: 2026-10-09

> Historical record of the first proof of concept. Client-specific names (subscriptions, resource groups, the Bicep repo) are replaced with neutral examples, and some config keys with their current names; see the README for current behaviour.

Status: Approved in conversation, pending written-spec review

## Purpose

Automatically produce infrastructure diagrams for an Azure platform from its Bicep, and validate that understanding against what is actually deployed in Azure.

One graph model serves two audiences:

- **Engineering** — a detailed, accurate topology plus a drift report (Bicep vs Azure).
- **Stakeholders** — a simplified logical architecture overview.

## Scope (PoC)

- **Source:** `~/Projects/bicep-project/infra`, read-only. No tool code is added to `bicep-project`.
- **Tool location:** `~/Projects/infra-diagrams`, a standalone local git repo. Hosting is decided after the PoC.
- **Environment:** `dev` end to end, plus the shared hub.

| Entry point | Params | Target |
|---|---|---|
| `network.bicep` | `environmentName=dev` (inline) | `rg-app-dev-weu` |
| `peering.bicep` | `environmentName=dev` (inline) | hub + dev VNets |
| `main.bicep` | `env/dev.bicepparam` | `rg-app-dev-weu` |
| `shared.bicep` | `env/shared.bicepparam` | `rg-app-shared-weu` |
| `workflow.bicep` | `env/workflow.bicepparam` | `rg-app-shared-weu` |
| `build-agents.bicep` | `env/build-agents.bicepparam` | `rg-app-shared-weu` |

- **Subscription:** `Contoso-Dev` (`0a1b2c3d-0000-4000-8000-000000000001`).
- **Resource groups queried in Azure:** `rg-app-dev-weu`, `rg-app-shared-weu`.

**Out of scope:** other environments and their hubs, RBAC-derived edges, draw.io/Mermaid output, CI automation and hosting.

## Approach

- **Python**, invoking `az` and `d2` as subprocesses. Authentication reuses the existing `az login`; no SDK auth setup.
- **Bicep model** — `az deployment sub what-if --result-format FullResourcePayloads` per entry point. This gives fully evaluated IDs and properties (modules flattened, loops expanded, conditions applied).
  - Exact arguments mirror the `az deployment sub create` steps in the delivery pipelines: `bicep-project/pipelines/deploy-shared.yml`, `deploy-workflow.yml` and `deploy-environment.yml`.
- **Azure model** — a Resource Graph query (`az graph query`) over the in-scope RGs.
- **Common pipeline** — both sources go through the same normaliser, so they can be joined on the lowercased ARM resource ID.

**Rejected alternatives:**

- **Offline `az bicep build` plus our own ARM expression evaluator** — reimplements the ARM template engine.
- **Compile plus fuzzy name matching** — inaccurate matching, weak edges and no credible validation.

## Architecture

```
config.yaml ──► extract ──► cache/*.json ──► normalise ──► graph ──┬─► diff ──► out/drift-report.md
                (Azure)    (raw, re-usable)                         └─► views ──► render ──► out/*.d2 ──► out/*.svg
```

| Unit | Responsibility | Depends on |
|---|---|---|
| `config.yaml` | Entry points (template, params file or inline params, scope, subscription, target), RGs to query, path to `bicep-project` | — |
| `views.yaml` | Filter, collapse and group rules; type → icon table; type → summary-group table; expected-implicit types | — |
| `extract_bicep` | Run what-if per entry point; write `cache/whatif/<entry>.json` | `az` |
| `extract_azure` | Run Resource Graph query; write `cache/arg.json` | `az graph` |
| `model` | `Node`/`Edge` types and the normaliser (raw JSON → graph) | pure |
| `diff` | Compare Bicep graph vs Azure graph; write drift report | `model` |
| `views` | Apply `views.yaml` to produce the detailed and summary views | `model` |
| `render_d2` | View → D2 text → SVG via the `d2` CLI (ELK layout) | `d2` |
| `cli` | `infradiag extract`, `infradiag diff`, `infradiag render` | all |

The cache split is deliberate:

- **Extract** is the only step that is slow or needs Azure access.
- **Everything after extract** runs offline in seconds against the cached JSON.
- **The same cached JSON**, trimmed down, serves as the test fixtures.

## Graph model and normalisation

### Node fields

- `id` — lowercased ARM resource ID; the join key.
- `type`
- `name`
- `subscription`
- `resource_group`
- `properties` — raw JSON.
- `sources` — subset of `{bicep, azure}`.
- `entry_points` — which Bicep entry point(s) produced it.
- `flags` — e.g. `unsupported`, `stub`.

### What-if handling (by `changeType`)

- **`Create` / `Modify` / `NoChange` / `Deploy`:** node from the `after` payload. For `Modify`, keep the property delta for the drift report.
- **`Ignore`:** not a Bicep node. Record it as a drift signal: present in the RG, absent from the template.
- **`Unsupported`:** node with no properties, flagged `unsupported`.

### Resource Graph handling

- One row from the `resources` table becomes one node.

### Inline children

On both sides, inline children are expanded into their own nodes so the two graphs align. Subnets inside `virtualNetworks.properties.subnets` are the minimum required.

### Edges

**Pass 1 — ID references**

- Walk every string in `properties`.
- Extract anything matching an ARM resource ID pattern.
- Record one edge per match, labelled with the JSON path (e.g. `properties.subnet.id`).
- Drop self-references and parent references; containment comes from the ID hierarchy instead.

**Pass 2 — endpoint references**

- Build a hostname → node map from known endpoint properties:
  - Key Vault `vaultUri`
  - App Configuration `endpoint`
  - SQL `fullyQualifiedDomainName`
  - Storage `primaryEndpoints.*`
  - Event Hubs `serviceBusEndpoint`
  - Azure AI services `endpoint`
- Scan Container App and Job env vars and secrets (including `keyVaultUrl`) for those hostnames.
- Emit one edge per match, of kind `endpoint`.

### Other rules

- **Stubs** — an edge target outside the scanned RGs becomes a stub node (e.g. `rg-app-platform-weu`, Microsoft-owned IDs).
- **Unevaluated values** — strings what-if could not evaluate (`[reference(...)]` and similar) are skipped.
- **Case** — all ID comparison is case-insensitive.

## Views

### Shared filtering

- **Hide:** role assignments, diagnostic settings, federated identity credentials, SQL firewall rules, private DNS record sets, Container App revisions.
- **Fold:** NICs into their owning private endpoint.
- **Collapse:** private DNS zone `virtualNetworkLinks` into a single zone → VNet edge.

### Detailed view (engineering)

- **Nesting:** subscription → RG → VNet → subnet.
  - Network-attached resources sit inside their subnet: private endpoints, the Container Apps environment, the VPN gateway, the DNS resolver and any AKS cluster.
  - All other resources sit directly in their RG.
- **Nodes:** icon, name and short type.
- **Private endpoints:** an edge to their target resource.
- **Drift styling:**

| Status | Style |
|---|---|
| In both | normal |
| Bicep only | dashed outline |
| Azure only | red |
| Stub | grey |

- If a single diagram is unreadable, split it into `detailed-hub.svg` and `detailed-dev.svg`. Decide on the real output.

### Summary view (stakeholders)

- **Logical groups (not RGs):**

| Group | Contents |
|---|---|
| Connectivity | VPN gateway, DNS private resolver |
| Networking | hub network resources |
| Workloads | Container Apps environment, apps and jobs, AKS |
| Data & integration | SQL, PostgreSQL, Storage, Event Hubs, Service Bus, Key Vault, App Config, Azure AI services |
| Observability | Log Analytics, App Insights, AMPLS |
| Platform | ACR, build agent pools |

- **Group assignment:** a type → group table in `views.yaml`, with per-name overrides.
- **Hidden:** private endpoints, subnets and managed identities. Private connectivity is shown as an edge style.
- **Edges:**
  - Mainly from the endpoint pass; duplicates merged.
  - Optional collapse of similar apps into one node (e.g. "Workers ×3").
- **No drift styling** — this view shows the intended architecture.

### Rendering

- **Tool:** D2 with the ELK layout engine, bundled with D2 and free.
- **Icons:** `https://icons.terrastruct.com/azure/...`, chosen via the type → icon table. A missing icon means no icon, not an error.
- **Outputs:**
  - `out/detailed.svg` (or the split pair)
  - `out/summary.svg`
  - the matching `.d2` sources, kept for debugging.

## Drift report (`out/drift-report.md`)

1. **Summary counts** — matched / Azure-only / Bicep-only / modified.
2. **In Azure, not in Bicep**
   - Each item is annotated with whether what-if `Ignore` corroborates it.
   - Types in the expected-implicit list (`views.yaml`) are listed separately and not counted as drift.
     - Examples: private endpoint NICs; the dashboard App Insights creates alongside itself.
3. **In Bicep, not in Azure** — resources the next deploy would create.
4. **Configuration drift** — what-if `Modify` results, with the changed property paths.
5. **Edge differences** — edges present on one side only.

## Error handling

- **A what-if fails for one entry point:**
  - Log it and continue with the others.
  - Record the failure in the cache.
  - The report states that this entry point's Bicep data is missing, so its resources are not mis-reported as Azure-only.
- **Missing or stale cache:** `diff` and `render` name the `extract` step to run.
- **`d2` not installed:** print an install hint. The `.d2` source is still written.

## Testing

pytest, with no Azure calls in automated tests.

| Area | What is tested |
|---|---|
| Normaliser | Trimmed real what-if and Resource Graph fixtures: ID normalisation, `changeType` handling, subnet expansion, both edge passes, stubs, skipping unevaluated values |
| Diff | Small hand-built graphs with known differences, covering each report section |
| Views | Filter, fold, collapse and group rules on a fixture graph |
| Renderer | Snapshot tests on the generated D2 text, not the SVG |

**Manual end-to-end test (PoC acceptance):**

- Run `extract`, then `diff`, then `render` against dev and the shared hub.
- This must produce:
  - two readable SVGs (detailed and summary)
  - a drift report the user has sanity-checked against their knowledge of the estate.

## Prerequisites

- `az` (present) with an active login to `Contoso-Dev`. Contributor is sufficient for what-if.
- `python3` (present).
- `d2` binary (to install).
