# infradiag

Architecture diagrams, a drift report and cost estimates for an Azure platform defined in Bicep.

infradiag previews your Bicep deployments with `az deployment sub what-if`, reads what is actually deployed from Azure Resource Graph, and compares the two. From that it writes, per subscription:

- `drift-report.md`: resources in Azure but not in Bicep and the reverse, properties the next deployment would change, and reference differences.
- `detailed.svg` and `summary.svg`: a topology diagram (subscription → resource group → resource, with subnets and references) and an architecture overview grouped by role. Their `.d2` sources are written alongside.
- `cost-report.md` and `.json`: monthly list-price estimates per resource group, plus actual spend if you ask for it.

It only reads from Azure. what-if, Resource Graph and Cost Management queries change nothing, and infradiag never writes into your Bicep project.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) and Python 3.12+
- [d2](https://d2lang.com/) to render diagrams (`brew install d2`)
- Azure CLI, logged in with `az login`. Every enabled subscription it can see is processed. what-if needs permission to validate a deployment (Contributor is enough). Reader is enough for Azure-only use.

Install the tool once and use it for any number of projects:

```bash
git clone <this repo> ~/src/infradiag
uv tool install --editable ~/src/infradiag      # puts `infradiag` on your PATH
# or, without installing: uv run --project ~/src/infradiag infradiag <command>
```

## Quick start on a new project

```bash
mkdir ~/diagrams/my-platform && cd ~/diagrams/my-platform
infradiag init                    # writes infradiag.yaml and views.yaml here
$EDITOR infradiag.yaml            # bicep_root, location and your subscriptions' entry points
infradiag extract                 # what-if per entry point + Resource Graph → cache/ (a few minutes)
infradiag diff                    # → out/<subscription>/drift-report.md
infradiag render                  # → out/<subscription>/detailed.svg, summary.svg
infradiag cost                    # → out/<subscription>/cost-report.md
```

With no subscriptions configured, `extract` still works: you get Azure-only diagrams of every resource group you can see, with no what-if or drift comparison. Add entry points to compare against Bicep.

The project folder can be anywhere, including inside your Bicep repo (add `cache/` and `out/` to its `.gitignore`). Every command looks for `infradiag.yaml` in the current directory. Pass `--config <path>` to use one elsewhere:

```bash
infradiag --config ~/diagrams/my-platform/infradiag.yaml diff
```

## Use

```bash
infradiag extract [--bicep | --azure]    # re-run only what-if or only Resource Graph
infradiag diff
infradiag render [--view detailed|summary|all]
infradiag cost [--actual [--from YYYY-MM-DD --to YYYY-MM-DD]] [--detail] [--refresh]
infradiag icons                          # download icons your views use into cache/icons/
infradiag init [DIR] [--force]           # starter infradiag.yaml + views.yaml
```

Every enabled subscription your `az login` can see is processed. To work on some of them, add `--subscription <name or ID>` to `extract`, `diff`, `render` or `cost`. Repeat it for more; names ignore case:

```bash
infradiag extract --subscription My-Dev-Subscription
infradiag render --subscription my-dev-subscription --subscription 00000000-0000-0000-0000-000000000000
```

Each subscription gets its own folder, so runs don't overwrite each other: `out/<subscription name>/` (made path-safe, with the ID appended if two subscriptions share a name) and `cache/<subscription id>/`. `extract` also writes `cache/subscriptions.json`, the subscriptions it found. That lets `diff`, `render` and `cost` run offline from `cache/`, so you can iterate on `views.yaml` without re-extracting. Without `--subscription` they skip subscriptions that haven't been extracted yet. Diagrams label each subscription `<name> (<id>)`.

Subscription names and IDs come from `az login` (`az account list`), never from config. The config names a subscription (display name or ID) only to give it settings, matched at run time; one you can't see is skipped with a note. A subscription with no Bicep entry points in the config is drawn from Azure only, from every resource group unless it lists some, with no what-if and no drift comparison. Its drift report says so, and its diagram titles end in "(Azure only)".

## Config reference (`infradiag.yaml`)

Relative paths are relative to the config file; `~` is expanded. `infradiag init` writes a commented starter.

| Key | Default | Meaning |
|---|---|---|
| `bicep_root` | the config's folder | Your Bicep project. what-if runs from here, so entry point templates and parameter files are relative to it. Only read, never written. |
| `location` | (none) | what-if deployment location; required for subscriptions with entry points unless they set their own. |
| `views` | infradiag's built-in generic Azure rules | Views file: what to hide, collapse, group and which icons to use; drift-report exclusions. |
| `cache_dir` | `cache` | Extract results, prices, actual cost and downloaded icons. |
| `out_dir` | `out` | Reports and diagrams. |
| `subscriptions` | `[]` | Per-subscription settings (below). |
| `cost` | GBP, no usage | Currency and usage assumptions for `infradiag cost` (see Cost). |

Unknown keys are rejected, so a typo doesn't silently fall back to a default.

Each `subscriptions` entry:

```yaml
subscriptions:
  - subscription: My-Dev-Subscription   # display name or ID, as az login shows it
    location: uksouth                   # optional; overrides the top-level location
    resource_groups: [rg-app-dev, rg-shared]   # resource groups to read from Azure; leave out to read them all
    entry_points:                       # one per subscription-scoped deployment your pipeline runs
      - name: main-dev                  # names the cache file and appears in reports
        deployment_name: infra-dev      # optional; default infra-<name>
        template: infra/main.bicep      # relative to bicep_root
        parameters:                     # each one is passed as --parameters: a file or name=value
          - infra/env/dev.bicepparam
          - imageTag=1.2.3              # pin what's deployed, or what-if reports every app as modified
        resource_groups: [rg-app-dev]   # RGs it deploys into; marks their resources unverified if what-if fails
```

Mirror your delivery pipeline's `az deployment sub create` calls so what-if previews what the next deployment would do.

## Views reference (`views.yaml`)

Types are lowercased ARM types and patterns use fnmatch (`*`, `?`). The built-in rules (`src/infradiag/defaults/views.yaml`, also what `init` copies) are generic Azure: hide noise such as role assignments and diagnostic settings, fold DNS zone links and VNet peerings into labelled edges, ignore read-only properties what-if reports as changes, and group resources into Connectivity, Networking, Workloads, Data & integration, Observability and Platform. Name-based rules start empty because names are project-specific:

- `out_of_scope_names`: resources that are neither drift nor drawn, such as other environments sharing a hub.
- `summary.name_overrides`: put a resource in a summary group by name pattern, ahead of its type.
- `summary.collapse_names`: draw many similarly named resources as one node, for example worker apps.

Other keys: `hide_types`, `hide_managed`, `hide_edges_to_types`, `collapse`, `implicit_types` (Azure-created resources that aren't drift), `ignore_modify_paths`, `icons`, and under `summary:` `groups`, `hide_types` and `nest_into_types`.

### Icons

Icons are paths on [icons.terrastruct.com](https://icons.terrastruct.com/). The ones the built-in views use ship with infradiag, so `render` needs no network. The SVGs embed them, so the output files are self-contained. For any other icon, run `infradiag icons` once to download it into `cache/icons/`; `render` fetches uncached icons from the internet and tells you when it does.

An icon in the views file can also be a path to your own SVG, relative to the views file (e.g. `icons/container-apps.svg`). The path must not contain spaces, because d2 can't bundle such files.

## Cost

`cost` estimates each subscription's monthly cost per environment from the [Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices). Each resource group is one environment, and shared resource groups get their own row. Estimates use pay-as-you-go list prices, so they leave out discounts, reservations, savings plans and free grants. It prices everything `extract` found, including Bicep resources not yet in Azure (shown as planned). Resource types are mapped to price meters by SKU and size in `src/infradiag/cost.py`. Anything unmapped, or billed by usage you haven't stated, is listed under "Not estimated" with the reason rather than guessed.

Prices are fetched once and cached in `cache/prices/<currency>/`, so re-running is offline; `--refresh` fetches them again. Currency and usage assumptions live in the config under `cost:`:

```yaml
cost:
  currency: GBP                 # default
  usage:                        # monthly usage per matching resource; first match wins per key
    - type: microsoft.operationalinsights/workspaces
      ingestion_gb: 20
    - type: microsoft.sql/servers/databases
      name: orders-*            # optional fnmatch on the resource name
      vcore_hours: 150
```

Usage keys: `ingestion_gb` (Log Analytics), `vcore_hours` (serverless SQL), `stored_gb` (blob storage), `operations` (Key Vault), `operations_millions` (Service Bus), `ingress_events_millions` (Event Hubs), `agent_hours` (Managed DevOps Pools), `data_processed_gb` (NAT gateway), `queries_millions` (private DNS zones). The report says which are missing.

Add `--actual` to also show what Azure Cost Management billed, read-only, next to the estimates per resource group. The default period is the last full month; set it with `--from YYYY-MM-DD --to YYYY-MM-DD`. Actuals are in the billing currency and cover every resource group in the subscription, including Azure-managed ones (`mc_*` for AKS, `me_*` for Container Apps). They need billing read access (Cost Management Reader). A subscription that refuses says so in its report and the run carries on. Actuals are cached in `cache/<subscription id>/actual-cost/`.

Add `--detail` for a per-resource breakdown under each resource group, in a collapsible section so the summary stays readable. The summary tables don't change. Each priced resource gets one row with its type, SKU and monthly subtotal, then one row per meter: the Retail Prices meter name, unit price, quantity and unit, and where the quantity comes from. That's `fixed` (730 hours, or the resource's own node count, capacity or storage size), `usage rule` (which `cost.usage` rule supplied it), or `default` (the resource doesn't say, so one unit is assumed). Parts left out of the estimate stay inline with the reason, and so do resources that aren't estimated at all.

With `--actual` too, a second read-only Cost Management query breaks the billed cost down per resource and meter. It's cached beside the per-resource-group one. Billed amounts sit in an Actual column next to the estimate for each extracted resource. A billed meter shares a row with an estimated one only when both have the same meter name. Other billed meters get their own `billed:` rows; the Markdown leaves out the ones billed at zero (free grants), and the JSON keeps them. Charges that match no extracted resource are listed separately with the reason: a resource the extract doesn't have, a resource group infradiag didn't extract, or an Azure-managed resource group such as `mc_*`. For example, an AKS cluster's estimate prices its nodes, but Azure bills them in its `mc_*` group, so they show up there. `cost-report.json` carries the same breakdown under `detail`.

```bash
infradiag cost --detail                                                  # estimate per resource and meter
infradiag cost --detail --actual --from 2026-09-01 --to 2026-09-30       # plus what was billed, side by side
```

## Keeping project configs out of this repo

Configs describe your platform, so keep them with that project or in a private folder. This repo ignores `local/`, so `local/<project>/infradiag.yaml` works if you'd rather keep them beside the tool:

```bash
uv run infradiag --config local/my-platform/infradiag.yaml diff
```

Design notes from the first proof of concept: `docs/superpowers/specs/2026-10-09-infra-diagrams-design.md`.

## Development

```bash
uv run pytest
```

[pre-commit](https://pre-commit.com) runs ruff (lint), markdownlint-cli2 and gitleaks, plus whitespace and YAML/TOML checks. It is in the dev dependency group, so `uv sync` installs it. Enable the git hook once per clone, then it runs on every commit:

```bash
uv run pre-commit install
uv run pre-commit run --all-files   # check the whole tree
```

Lint settings live in `pyproject.toml` (`[tool.ruff]`) and `.markdownlint-cli2.yaml`. If gitleaks flags something that isn't a secret, allowlist it in `.gitleaks.toml` with a comment saying why.

## License

MIT. See [LICENSE](LICENSE).
