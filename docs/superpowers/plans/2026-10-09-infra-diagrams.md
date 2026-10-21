# Infra Diagrams PoC Implementation Plan

> Historical record of the first proof of concept. Client-specific names (subscriptions, resource groups, the Bicep repo) are replaced with neutral examples, and some config keys with their current names; see the README for current behaviour.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python CLI (`infradiag`) that builds a resource graph of a shared hub + `dev` environment from Bicep what-if output and from Azure Resource Graph, reports drift between them, and renders detailed and summary diagrams as D2 → SVG.

**Architecture:** `extract` runs `az deployment sub what-if` per Bicep entry point and one Resource Graph query, caching raw JSON. Everything after that is offline:

1. Both sources are normalised into the same `Graph`, with nodes keyed by lowercased ARM resource ID.
2. Edges are derived by scanning properties for resource IDs and for service hostnames.
3. `diff` compares the two graphs.
4. `views` turns the merged graph into renderer-agnostic `View` trees.
5. `render_d2` emits D2 text and shells out to the `d2` CLI.

**Tech Stack:**

- Python ≥ 3.12 (local: 3.14), managed with `uv`.
- PyYAML.
- pytest.
- Azure CLI (`az`, with the `resource-graph` extension).
- D2 (`d2` CLI, ELK layout).

**Spec:** `docs/superpowers/specs/2026-10-09-infra-diagrams-design.md`

## Global Constraints

- **Location:** the tool lives in `~/Projects/infra-diagrams`. Nothing is ever written under `~/Projects/bicep-project`; it is only read, and used as the working directory for `az`.
- **Dependencies:** runtime dependencies are `pyyaml` only; dev dependencies are `pytest` only. `az` and `d2` are invoked as subprocesses — no Azure SDKs.
- **Authentication:** reuse the existing `az login`. The subscription is `Contoso-Dev` (`0a1b2c3d-0000-4000-8000-000000000001`) and the location is `westeurope`.
- **Tests:** automated tests make no Azure calls and no network calls. The single exception is the optional real-`d2` syntax test, which is skipped when `d2` is absent.
- **Resource IDs:** always compared lowercased.
- **Gitignored:** `cache/` and `out/`.
- **Commits:** end every commit message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

These inputs are not exercised by the main-path tests, and a person using the tool would be hurt most if they broke:

1. **Case-mismatched IDs.** The same resource arrives with different ID casing from what-if (`Microsoft.Network/...`, `resourceGroups`) and from Resource Graph (`microsoft.network/...`). It must be one matched node, not one Bicep-only plus one Azure-only. Tested in Task 5.
2. **Resource Graph paging.** A query returning more than one page (`skip_token`) must yield all rows, not silently truncate to the first 1000. Tested in Task 7.
3. **One what-if failing.** If one entry point's what-if fails (bad params, expired login mid-run):
   - the other entry points are still extracted;
   - Azure resources in the failed entry point's resource groups are reported as *unverified*, not as drift.

   Tested in Tasks 7 and 8.
4. **One resource from two sources.** The same resource emitted twice — by two entry points, or both inline in its parent and as a child resource (VNet peerings, subnets) — must become one node with merged entry points. Tested in Task 4.
5. **D2-unsafe names.** Names containing dots, quotes or other D2-significant characters (e.g. `privatelink.vaultcore.azure.net`) must render as one labelled node, never as accidental nesting or a syntax error. Tested in Task 11.

## Facts discovered while planning

These came from a real `what-if` against `network.bicep`/dev and from Resource Graph queries. The code below depends on them.

**What-if output**

- Running with `--result-format FullResourcePayloads --no-pretty-print -o json` returns `{status, error, changes[], potentialChanges, diagnostics}`.
- Each change has `changeType`, `resourceId`, `before`, `after` and `delta`.
- **`Ignore` is per entry point.** Every what-if lists all *other* resources in its target RGs as `Ignore`, including those owned by other entry points. `Ignore` therefore only means "not in Bicep" for IDs that no entry point produced.
- **Inline subnets have no `id`.** In what-if `after`, VNet subnets inline in `properties.subnets[]` have a `name` but no `id`, so their IDs must be synthesised. Resource Graph subnets do have an `id`.
- **The VNet always shows `Modify`.** The network what-if deletes `properties.virtualNetworkPeerings`, because peerings are deployed separately by `peering.bicep`. That path must be ignored in configuration drift.
- **`delta` format.** Each item has `path`, `propertyChangeType` and optional nested `children`.

**Resource Graph**

- The `resources` table omits many child types: Event Hubs hubs, Service Bus topics, SQL firewall rules, DNS records and role assignments.
- A Bicep resource whose what-if says `NoChange`/`Modify` but which Resource Graph lacks is therefore **not drift**. It exists (what-if compared it with live state); it just isn't indexed. Only what-if `Create` means "not in Azure".
- `managedBy` is `""` when empty, and is set on PE NICs and the SQL `master` database.

**Resources not deployed by this Bicep**

These dev container apps come from other repos, so expect them as Azure-only:

- `ca-web-dev-weu`
- `ca-reports-dev-weu`
- `ca-notify-dev-weu`

**Container app configuration**

- Container app env vars hold only the App Configuration endpoint and the App Insights connection string.
- The registry `server` is the ACR login server.
- Most app → service links therefore aren't visible in resource config.

**Image tag**

- Currently deployed: `1.2.3`. `main.bicep` defaults to `latest`, so config pins the deployed tag to avoid every app showing as modified.

**Icons**

- Terrastruct Azure icon paths were verified with `curl`.
- Container Apps, Private Endpoint and NAT Gateway have no icon in that set.

## File Structure

```
infra-diagrams/
  pyproject.toml
  .gitignore
  README.md
  config.yaml                      # entry points + RGs (dev + shared hub)
  views.yaml                       # filter/collapse/group/icon/drift rules
  src/infradiag/
    __init__.py
    config.py                      # load config.yaml → Config/EntryPoint
    cache.py                       # CacheMissing, read_json, write_json
    azcli.py                       # run_az subprocess wrapper, AzError
    ids.py                         # ARM ID parse/normalise/find/resolve
    model.py                       # Node, Edge, Graph, merge_graphs
    normalise.py                   # what-if/ARG JSON → Graph (+ WhatIfSignals)
    edges.py                       # reference + endpoint edge passes
    extract_bicep.py               # what-if per entry point → cache
    extract_azure.py               # Resource Graph → cache
    rules.py                       # load views.yaml → Rules
    diff.py                        # DriftReport + markdown
    views.py                       # filter_graph, build_detailed, build_summary
    render_d2.py                   # View → D2 text → SVG
    cli.py                         # argparse entry point
  tests/
    __init__.py
    factories.py                   # shared test builders
    test_config.py  test_ids.py  test_model.py  test_normalise.py
    test_edges.py   test_extract.py  test_diff.py  test_views.py
    test_render_d2.py  test_cli.py
```

---

### Task 1: Project scaffold and config loader

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `config.yaml`
- Create: `src/infradiag/__init__.py`
- Create: `src/infradiag/config.py`
- Create: `src/infradiag/cache.py`
- Create: `tests/__init__.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `config.load_config(path: Path) -> Config`
  - `config.ConfigError(Exception)`
  - `@dataclass(frozen=True) EntryPoint(name: str, deployment_name: str, template: str, parameters: tuple[str, ...], resource_groups: tuple[str, ...])`
  - `@dataclass(frozen=True) Config(bicep_root: Path, subscription: str, location: str, resource_groups: tuple[str, ...], entry_points: tuple[EntryPoint, ...], cache_dir: Path, out_dir: Path)`
  - Resource group names and the subscription are lowercased.
  - `cache.CacheMissing(Exception)`
  - `cache.write_json(path: Path, data) -> None`
  - `cache.read_json(path: Path, hint: str)`, which raises `CacheMissing` naming the command in `hint`.

- [ ] **Step 1: Create project files**

`pyproject.toml`:

```toml
[project]
name = "infradiag"
version = "0.1.0"
description = "Infrastructure diagrams and drift report from Bicep what-if and Azure Resource Graph"
requires-python = ">=3.12"
dependencies = ["pyyaml>=6"]

[project.scripts]
infradiag = "infradiag.cli:main"

[dependency-groups]
dev = ["pytest>=8"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/infradiag"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
```

`.gitignore`:

```
.venv/
__pycache__/
cache/
out/
```

`src/infradiag/__init__.py` and `tests/__init__.py`: empty files.

`config.yaml`:

```yaml
# Which Bicep deployments to preview (what-if) and which resource groups to read from Azure.
bicep_root: ~/Projects/bicep-project
subscription: 0a1b2c3d-0000-4000-8000-000000000001   # Contoso-Dev
location: westeurope
resource_groups: [rg-app-dev-weu, rg-app-shared-weu]

# Mirrors the `az deployment sub create` steps in bicep-project's deploy pipelines
# (pipelines/deploy-shared.yml, pipelines/deploy-workflow.yml and pipelines/deploy-environment.yml).
# template/parameters paths are relative to bicep_root.
# resource_groups = the RGs an entry point deploys into (used to mark results unverified if it fails).
entry_points:
  - name: shared
    deployment_name: infra-shared
    template: infra/shared.bicep
    parameters: [infra/env/shared.bicepparam]
    resource_groups: [rg-app-shared-weu]
  - name: build-agents
    deployment_name: infra-build-agents
    template: infra/build-agents.bicep
    parameters: [infra/env/build-agents.bicepparam]
    resource_groups: [rg-app-shared-weu]
  - name: workflow
    deployment_name: infra-workflow
    template: infra/workflow.bicep
    parameters: [infra/env/workflow.bicepparam]
    resource_groups: [rg-app-shared-weu]
  - name: network-dev
    deployment_name: infra-network-dev
    template: infra/network.bicep
    parameters: [environmentName=dev]
    resource_groups: [rg-app-dev-weu]
  - name: peering-dev
    deployment_name: infra-peering-dev
    template: infra/peering.bicep
    parameters: [environmentName=dev]
    resource_groups: [rg-app-dev-weu, rg-app-shared-weu]
  - name: main-dev
    deployment_name: infra-dev
    template: infra/main.bicep
    # Pin the image tag currently deployed to dev, otherwise what-if reports every app as modified.
    parameters:
      - infra/env/dev.bicepparam
      - apiImageTag=1.2.3
      - webImageTag=1.2.3
      - workerImageTag=1.2.3
    # main.bicep also writes DNS zone links into the hub's zones in the shared RG.
    resource_groups: [rg-app-dev-weu, rg-app-shared-weu]
```

- [ ] **Step 2: Write the failing tests**

`tests/test_config.py`:

```python
from pathlib import Path

import pytest

from infradiag.cache import CacheMissing, read_json, write_json
from infradiag.config import ConfigError, load_config

VALID = """
bicep_root: ~/Projects/bicep-project
subscription: 0A1B2C3D-0000-4000-8000-000000000001
location: westeurope
resource_groups: [RG-APP-DEV-WEU]
entry_points:
  - name: main-dev
    deployment_name: infra-dev
    template: infra/main.bicep
    parameters: [infra/env/dev.bicepparam, imageTag=1]
    resource_groups: [rg-app-dev-weu]
  - name: network-dev
    template: infra/network.bicep
    resource_groups: [rg-app-dev-weu]
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_loads_entry_points_and_lowercases_ids(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert cfg.subscription == "0a1b2c3d-0000-4000-8000-000000000001"
    assert cfg.resource_groups == ("rg-app-dev-weu",)
    assert cfg.bicep_root == Path("~/Projects/bicep-project").expanduser()
    main = cfg.entry_points[0]
    assert main.parameters == ("infra/env/dev.bicepparam", "imageTag=1")
    assert main.deployment_name == "infra-dev"


def test_defaults_deployment_name_and_parameters(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    network = cfg.entry_points[1]
    assert network.deployment_name == "infra-network-dev"
    assert network.parameters == ()


def test_cache_and_out_dirs_are_relative_to_config_file(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert cfg.cache_dir == tmp_path / "cache"
    assert cfg.out_dir == tmp_path / "out"


def test_missing_key_names_the_key(tmp_path):
    with pytest.raises(ConfigError, match="entry_points\\[0\\]: missing required key 'template'"):
        load_config(write(tmp_path, VALID.replace("    template: infra/main.bicep\n", "")))


def test_duplicate_entry_point_names_rejected(tmp_path):
    text = VALID.replace("name: network-dev", "name: main-dev")
    with pytest.raises(ConfigError, match="duplicate entry point names: main-dev"):
        load_config(write(tmp_path, text))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(tmp_path / "nope.yaml")


def test_cache_round_trip_and_missing_hint(tmp_path):
    write_json(tmp_path / "a" / "b.json", {"x": 1})
    assert read_json(tmp_path / "a" / "b.json", "unused") == {"x": 1}
    with pytest.raises(CacheMissing, match="run `infradiag extract --azure`"):
        read_json(tmp_path / "missing.json", "infradiag extract --azure")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd ~/Projects/infra-diagrams && uv sync && uv run pytest tests/test_config.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'infradiag.cache'`.

- [ ] **Step 4: Implement**

`src/infradiag/config.py`:

```python
"""Load config.yaml: which Bicep entry points to what-if, and which RGs to read from Azure."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class EntryPoint:
    name: str
    deployment_name: str
    template: str
    parameters: tuple[str, ...]
    resource_groups: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    bicep_root: Path
    subscription: str
    location: str
    resource_groups: tuple[str, ...]
    entry_points: tuple[EntryPoint, ...]
    cache_dir: Path
    out_dir: Path


def _require(data: dict, key: str, where: str):
    if key not in data or data[key] in (None, "", []):
        raise ConfigError(f"{where}: missing required key '{key}'")
    return data[key]


def load_config(path: Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    data = yaml.safe_load(path.read_text()) or {}

    entry_points = []
    for i, raw in enumerate(_require(data, "entry_points", "config")):
        where = f"entry_points[{i}]"
        name = _require(raw, "name", where)
        entry_points.append(
            EntryPoint(
                name=name,
                deployment_name=raw.get("deployment_name") or f"infra-{name}",
                template=_require(raw, "template", where),
                parameters=tuple(str(p) for p in raw.get("parameters") or []),
                resource_groups=tuple(rg.lower() for rg in _require(raw, "resource_groups", where)),
            )
        )

    names = [ep.name for ep in entry_points]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ConfigError(f"duplicate entry point names: {', '.join(dupes)}")

    base = path.parent
    return Config(
        bicep_root=Path(_require(data, "bicep_root", "config")).expanduser(),
        subscription=str(_require(data, "subscription", "config")).lower(),
        location=_require(data, "location", "config"),
        resource_groups=tuple(rg.lower() for rg in _require(data, "resource_groups", "config")),
        entry_points=tuple(entry_points),
        cache_dir=base / data.get("cache_dir", "cache"),
        out_dir=base / data.get("out_dir", "out"),
    )
```

`src/infradiag/cache.py`:

```python
"""Raw extract results live as JSON under the cache dir so everything after extract runs offline."""

from __future__ import annotations

import json
from pathlib import Path


class CacheMissing(Exception):
    pass


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1))


def read_json(path: Path, hint: str):
    if not path.exists():
        raise CacheMissing(f"{path} not found — run `{hint}`")
    return json.loads(path.read_text())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`

Expected: 7 passed.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock .gitignore config.yaml src tests
git commit -m "Scaffold infradiag with config and cache helpers

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: ARM resource ID helpers

**Files:**
- Create: `src/infradiag/ids.py`
- Test: `tests/test_ids.py`

**Interfaces:**
- Produces:
  - `normalise_id(resource_id: str) -> str` — strips whitespace and trailing `/`, then lowercases.
  - `@dataclass(frozen=True) IdParts(subscription: str, resource_group: str, type: str, name: str, parent_id: str | None)` — all fields lowercased.
  - `parse_id(resource_id: str) -> IdParts | None` — returns `None` for anything that isn't a resource-group-scoped provider resource.
  - `find_ids(text: str) -> list[str]` — the normalised IDs found inside arbitrary text.
  - `top_level_id(resource_id: str) -> str`
  - `resolve(resource_id: str, known: set[str]) -> str` — returns the longest prefix of the ID (self, parent, grandparent …) that is in `known`, otherwise `top_level_id`.

- [ ] **Step 1: Write the failing tests**

`tests/test_ids.py`:

```python
from infradiag.ids import find_ids, normalise_id, parse_id, resolve, top_level_id

SUB = "0a1b2c3d-0000-4000-8000-000000000001"
VNET = f"/subscriptions/{SUB}/resourceGroups/rg-app-dev-weu/providers/Microsoft.Network/virtualNetworks/vnet-app-dev-weu"


def test_normalise_lowercases_and_strips_trailing_slash():
    assert normalise_id(VNET + "/ ") == VNET.lower()


def test_parse_top_level():
    parts = parse_id(VNET)
    assert parts.subscription == SUB
    assert parts.resource_group == "rg-app-dev-weu"
    assert parts.type == "microsoft.network/virtualnetworks"
    assert parts.name == "vnet-app-dev-weu"
    assert parts.parent_id is None


def test_parse_child():
    parts = parse_id(VNET + "/subnets/snet-aca")
    assert parts.type == "microsoft.network/virtualnetworks/subnets"
    assert parts.name == "snet-aca"
    assert parts.parent_id == VNET.lower()


def test_parse_extension_resource_uses_last_provider():
    kv = f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/kv"
    parts = parse_id(kv + "/providers/Microsoft.Insights/diagnosticSettings/diag")
    assert parts.type == "microsoft.insights/diagnosticsettings"
    assert parts.name == "diag"
    assert parts.parent_id == kv.lower()


def test_parse_rejects_non_resource_ids():
    assert parse_id(f"/subscriptions/{SUB}/resourceGroups/rg-app-dev-weu") is None
    assert parse_id(f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/abc") is None
    assert parse_id("not an id") is None


def test_find_ids_in_free_text():
    text = f"see https://management.azure.com{VNET}/subnets/snet-pe?api-version=1 and {VNET.upper()}"
    assert find_ids(text) == [VNET.lower() + "/subnets/snet-pe", VNET.lower()]


def test_find_ids_trims_dangling_type_segment():
    assert find_ids(VNET + "/subnets") == [VNET.lower()]


def test_top_level_id():
    assert top_level_id(VNET + "/subnets/snet-aca") == VNET.lower()


def test_resolve_prefers_longest_known_prefix():
    subnet = VNET.lower() + "/subnets/snet-aca"
    assert resolve(subnet + "/whatever/x", {VNET.lower(), subnet}) == subnet
    assert resolve(subnet, {VNET.lower()}) == VNET.lower()
    assert resolve(subnet, set()) == VNET.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ids.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'infradiag.ids'`.

- [ ] **Step 3: Implement**

`src/infradiag/ids.py`:

```python
"""ARM resource ID helpers. IDs are always compared lowercased."""

from __future__ import annotations

import re
from dataclasses import dataclass

_ID_RE = re.compile(
    r"/subscriptions/[0-9a-f-]{36}/resourcegroups/[^/\s\"'?#]+/providers/[^\s\"'?#,;]+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class IdParts:
    subscription: str
    resource_group: str
    type: str
    name: str
    parent_id: str | None


def normalise_id(resource_id: str) -> str:
    return resource_id.strip().rstrip("/").lower()


def parse_id(resource_id: str) -> IdParts | None:
    rid = normalise_id(resource_id)
    parts = rid.split("/")
    if (
        len(parts) < 9
        or parts[0] != ""
        or parts[1] != "subscriptions"
        or parts[3] != "resourcegroups"
        or parts[5] != "providers"
    ):
        return None
    # Extension resources (diagnostic settings, scoped role assignments) nest a second
    # /providers/ segment; the last one names the resource's own type.
    last = max(i for i, p in enumerate(parts) if p == "providers")
    rest = parts[last + 2 :]
    if not rest or len(rest) % 2:
        return None
    rtype = "/".join([parts[last + 1], *rest[0::2]])
    if len(rest) > 2:
        parent = "/".join(parts[:-2])
    elif last > 5:
        parent = "/".join(parts[:last])
    else:
        parent = None
    return IdParts(parts[2], parts[4], rtype, rest[-1], parent)


def find_ids(text: str) -> list[str]:
    found = []
    for match in _ID_RE.finditer(text):
        rid = normalise_id(match.group(0))
        while parse_id(rid) is None and rid.count("/") > 8:
            rid = rid.rsplit("/", 1)[0]
        if parse_id(rid) is not None:
            found.append(rid)
    return found


def top_level_id(resource_id: str) -> str:
    return "/".join(normalise_id(resource_id).split("/")[:9])


def resolve(resource_id: str, known: set[str]) -> str:
    rid = normalise_id(resource_id)
    candidate: str | None = rid
    while candidate:
        if candidate in known:
            return candidate
        parts = parse_id(candidate)
        candidate = parts.parent_id if parts else None
    return top_level_id(rid)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ids.py -v`

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add src/infradiag/ids.py tests/test_ids.py
git commit -m "Add ARM resource ID helpers

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Graph model

**Files:**
- Create: `src/infradiag/model.py`
- Create: `tests/factories.py`
- Test: `tests/test_model.py`

**Interfaces:**
- Consumes:
  - `ids.parse_id`
  - `ids.normalise_id`
- Produces:
  - Constants: `BICEP = "bicep"`, `AZURE = "azure"`, `REF = "ref"`, `ENDPOINT = "endpoint"`, `COLLAPSED = "collapsed"`, `STUB = "stub"`, `UNSUPPORTED = "unsupported"`.
  - `@dataclass Node(id, type, name, subscription, resource_group, properties: dict, sources: set[str], entry_points: set[str], flags: set[str], managed_by: str | None)`.
    - `Node.parent_id -> str | None` (property).
    - `Node.from_id(resource_id, *, name=None, **kwargs) -> Node`. The display name is the last `/` segment of `name` (or of the ID). It raises `ValueError` for non-resource IDs.
  - `@dataclass(frozen=True, order=True) Edge(source: str, target: str, kind: str, label: str)`.
  - `@dataclass Graph(nodes: dict[str, Node], edges: set[Edge])`.
    - `Graph.add_node(node) -> Node` merges into any existing node with the same id:
      - sets are unioned;
      - existing properties are kept unless they are empty;
      - `managed_by` keeps the first non-empty value;
      - `STUB` is dropped once the node has any source.
  - `merge_graphs(*graphs) -> Graph` returns a new graph and never mutates its inputs.
  - `tests/factories.py` provides:
    - `SUB`
    - `rid(rg, provider_path) -> str`
    - `node(resource_id, sources=("bicep","azure"), **kw) -> Node`

- [ ] **Step 1: Write the failing tests**

`tests/factories.py`:

```python
"""Shared builders for tests. Shapes mirror real what-if and Resource Graph output."""

from infradiag.model import Node

SUB = "0a1b2c3d-0000-4000-8000-000000000001"
DEV = "rg-app-dev-weu"
SHARED = "rg-app-shared-weu"


def rid(rg: str, provider_path: str) -> str:
    return f"/subscriptions/{SUB}/resourceGroups/{rg}/providers/{provider_path}"


def node(resource_id: str, sources=("bicep", "azure"), **kwargs) -> Node:
    return Node.from_id(resource_id, sources=set(sources), **kwargs)
```

`tests/test_model.py`:

```python
import pytest

from infradiag.model import AZURE, BICEP, REF, STUB, Edge, Graph, Node, merge_graphs
from tests.factories import DEV, rid

VNET = rid(DEV, "Microsoft.Network/virtualNetworks/vnet-app-dev-weu")


def test_from_id_fills_parts_and_lowercases():
    n = Node.from_id(VNET, name="vnet-app-dev-weu")
    assert n.id == VNET.lower()
    assert n.type == "microsoft.network/virtualnetworks"
    assert n.resource_group == DEV
    assert n.name == "vnet-app-dev-weu"


def test_from_id_uses_last_name_segment():
    db = rid(DEV, "Microsoft.Sql/servers/sql-1/databases/app-db")
    assert Node.from_id(db, name="sql-1/app-db").name == "app-db"
    assert Node.from_id(db).name == "app-db"


def test_from_id_rejects_non_resource_ids():
    with pytest.raises(ValueError):
        Node.from_id("/subscriptions/x/resourceGroups/rg")


def test_parent_id():
    assert Node.from_id(VNET + "/subnets/snet-aca").parent_id == VNET.lower()
    assert Node.from_id(VNET).parent_id is None


def test_add_node_merges_sources_and_keeps_non_empty_properties():
    g = Graph()
    g.add_node(Node.from_id(VNET, sources={BICEP}, entry_points={"network-dev"}, properties={"a": 1}))
    merged = g.add_node(Node.from_id(VNET.upper(), sources={AZURE}, properties={"b": 2}, managed_by="x"))
    assert merged.sources == {BICEP, AZURE}
    assert merged.entry_points == {"network-dev"}
    assert merged.properties == {"a": 1}
    assert merged.managed_by == "x"
    assert len(g.nodes) == 1


def test_add_node_fills_empty_properties():
    g = Graph()
    g.add_node(Node.from_id(VNET, sources={BICEP}))
    assert g.add_node(Node.from_id(VNET, sources={AZURE}, properties={"b": 2})).properties == {"b": 2}


def test_stub_flag_dropped_when_real_source_arrives():
    g = Graph()
    g.add_node(Node.from_id(VNET, flags={STUB}))
    assert g.add_node(Node.from_id(VNET, sources={AZURE})).flags == set()


def test_merge_graphs_does_not_mutate_inputs():
    a, b = Graph(), Graph()
    a.add_node(Node.from_id(VNET, sources={BICEP}))
    b.add_node(Node.from_id(VNET, sources={AZURE}))
    b.edges.add(Edge(VNET.lower(), VNET.lower() + "/subnets/s", REF, "x"))
    merged = merge_graphs(a, b)
    assert merged.nodes[VNET.lower()].sources == {BICEP, AZURE}
    assert a.nodes[VNET.lower()].sources == {BICEP}
    assert len(merged.edges) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_model.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'infradiag.model'`.

- [ ] **Step 3: Implement**

`src/infradiag/model.py`:

```python
"""The graph both sources normalise into: nodes keyed by lowercased ARM ID, plus typed edges."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .ids import normalise_id, parse_id

BICEP = "bicep"
AZURE = "azure"

REF = "ref"              # a resource ID found in another resource's properties
ENDPOINT = "endpoint"    # a service hostname/key found in a container app's config
COLLAPSED = "collapsed"  # a node replaced by an edge in a view (DNS links, peerings)

STUB = "stub"                # referenced but outside the scanned RGs
UNSUPPORTED = "unsupported"  # what-if couldn't evaluate this resource


@dataclass
class Node:
    id: str
    type: str
    name: str
    subscription: str
    resource_group: str
    properties: dict = field(default_factory=dict)
    sources: set[str] = field(default_factory=set)
    entry_points: set[str] = field(default_factory=set)
    flags: set[str] = field(default_factory=set)
    managed_by: str | None = None

    @property
    def parent_id(self) -> str | None:
        parts = parse_id(self.id)
        return parts.parent_id if parts else None

    @classmethod
    def from_id(cls, resource_id: str, *, name: str | None = None, **kwargs) -> Node:
        parts = parse_id(resource_id)
        if parts is None:
            raise ValueError(f"not a resource-group-scoped resource ID: {resource_id}")
        return cls(
            id=normalise_id(resource_id),
            type=parts.type,
            name=(name or parts.name).split("/")[-1],
            subscription=parts.subscription,
            resource_group=parts.resource_group,
            **kwargs,
        )


@dataclass(frozen=True, order=True)
class Edge:
    source: str
    target: str
    kind: str
    label: str


@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: set[Edge] = field(default_factory=set)

    def add_node(self, node: Node) -> Node:
        existing = self.nodes.get(node.id)
        if existing is None:
            self.nodes[node.id] = node
            return node
        if not existing.properties and node.properties:
            existing.properties = node.properties
        existing.sources |= node.sources
        existing.entry_points |= node.entry_points
        existing.flags |= node.flags
        existing.managed_by = existing.managed_by or node.managed_by
        if existing.sources:
            existing.flags.discard(STUB)
        return existing


def merge_graphs(*graphs: Graph) -> Graph:
    merged = Graph()
    for graph in graphs:
        for n in graph.nodes.values():
            merged.add_node(
                replace(n, sources=set(n.sources), entry_points=set(n.entry_points), flags=set(n.flags))
            )
        merged.edges |= graph.edges
    return merged
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_model.py -v`

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/infradiag/model.py tests/factories.py tests/test_model.py
git commit -m "Add graph model with merge semantics

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Normalise what-if output

**Files:**
- Create: `src/infradiag/normalise.py`
- Modify: `tests/factories.py` (append what-if builders)
- Test: `tests/test_normalise.py`

**Interfaces:**
- Consumes:
  - `model.Node`, `Graph`, `BICEP`, `UNSUPPORTED`
  - `ids.parse_id`, `normalise_id`
- Produces:
  - `INLINE_CHILDREN: dict[str, tuple[str, ...]] = {"microsoft.network/virtualnetworks": ("subnets", "virtualNetworkPeerings")}`
  - `@dataclass(frozen=True) PropertyChange(path: str, change: str)`
  - `@dataclass WhatIfSignals`, with fields:
    - `ignored: dict[str, set[str]]` — id → entry points reporting `Ignore`.
    - `modified: dict[str, list[PropertyChange]]`
    - `change_types: dict[str, str]` — id → what-if changeType.
    - `failed: dict[str, str]` — entry point → error.
  - `expand_inline_children(graph: Graph, node: Node) -> None`
  - `flatten_delta(items: list | None, prefix: str = "") -> list[PropertyChange]`
  - `graph_from_whatif(results: dict[str, dict]) -> tuple[Graph, WhatIfSignals]` — keys are entry point names.

- [ ] **Step 1: Append what-if builders to `tests/factories.py`**

```python
def whatif_change(change_type: str, resource_id: str, name: str | None = None,
                  properties: dict | None = None, delta: list | None = None) -> dict:
    after = None
    if change_type not in ("Ignore", "Unsupported", "Delete"):
        after = {"id": resource_id, "name": name or resource_id.rsplit("/", 1)[-1],
                 "properties": properties or {}}
    return {"changeType": change_type, "resourceId": resource_id, "before": None,
            "after": after, "delta": delta}


def whatif_result(*changes: dict) -> dict:
    return {"status": "Succeeded", "error": None, "changes": list(changes)}
```

- [ ] **Step 2: Write the failing tests**

`tests/test_normalise.py`:

```python
from infradiag.model import AZURE, BICEP, UNSUPPORTED
from infradiag.normalise import PropertyChange, flatten_delta, graph_from_whatif
from tests.factories import DEV, SHARED, rid, whatif_change, whatif_result

VNET = rid(DEV, "Microsoft.Network/virtualNetworks/vnet-app-dev-weu")
HUB = rid(SHARED, "Microsoft.Network/virtualNetworks/vnet-app-shared-weu")
KV = rid(DEV, "Microsoft.KeyVault/vaults/kv-app-dev-weu")
NSG = rid(DEV, "Microsoft.Network/networkSecurityGroups/nsg-1")


def test_node_change_types_become_bicep_nodes():
    graph, signals = graph_from_whatif({"main": whatif_result(
        whatif_change("Create", KV),
        whatif_change("NoChange", NSG),
    )})
    assert set(graph.nodes) == {KV.lower(), NSG.lower()}
    assert graph.nodes[KV.lower()].sources == {BICEP}
    assert graph.nodes[KV.lower()].entry_points == {"main"}
    assert signals.change_types == {KV.lower(): "Create", NSG.lower(): "NoChange"}


def test_resource_group_changes_are_skipped():
    rg = f"/subscriptions/x/resourceGroups/{DEV}"
    graph, _ = graph_from_whatif({"net": whatif_result(whatif_change("NoChange", rg))})
    assert graph.nodes == {}


def test_ignore_is_a_signal_not_a_node():
    graph, signals = graph_from_whatif({"net": whatif_result(whatif_change("Ignore", KV))})
    assert graph.nodes == {}
    assert signals.ignored == {KV.lower(): {"net"}}


def test_unsupported_is_flagged_node():
    graph, _ = graph_from_whatif({"main": whatif_result(whatif_change("Unsupported", KV))})
    assert graph.nodes[KV.lower()].flags == {UNSUPPORTED}


def test_inline_subnets_without_id_get_synthesised_ids():
    vnet = whatif_change("Modify", VNET, properties={
        "subnets": [{"name": "snet-aca", "properties": {"networkSecurityGroup": {"id": NSG}}}],
    })
    graph, _ = graph_from_whatif({"net": whatif_result(vnet)})
    subnet = graph.nodes[VNET.lower() + "/subnets/snet-aca"]
    assert subnet.type == "microsoft.network/virtualnetworks/subnets"
    assert subnet.name == "snet-aca"
    assert subnet.properties == {"networkSecurityGroup": {"id": NSG}}
    assert subnet.sources == {BICEP}


def test_same_resource_from_two_entry_points_and_inline_is_one_node():
    peering_id = HUB + "/virtualNetworkPeerings/peer-hub-to-dev"
    shared = whatif_result(whatif_change("NoChange", HUB, properties={
        "virtualNetworkPeerings": [{"name": "peer-hub-to-dev", "properties": {}}],
    }))
    peering = whatif_result(
        whatif_change("NoChange", peering_id, name="vnet-app-shared-weu/peer-hub-to-dev",
                      properties={"remoteVirtualNetwork": {"id": VNET}}),
        whatif_change("NoChange", HUB),
    )
    graph, _ = graph_from_whatif({"shared": shared, "peering": peering})
    assert [i for i in graph.nodes if "peerings" in i] == [peering_id.lower()]
    peer = graph.nodes[peering_id.lower()]
    assert peer.entry_points == {"shared", "peering"}
    assert peer.properties == {"remoteVirtualNetwork": {"id": VNET}}
    assert graph.nodes[HUB.lower()].entry_points == {"shared", "peering"}


def test_modify_records_flattened_delta():
    delta = [
        {"path": "properties.virtualNetworkPeerings", "propertyChangeType": "Delete", "children": None},
        {"path": "properties.subnets", "propertyChangeType": "Array", "children": [
            {"path": "0", "propertyChangeType": "Modify", "children": [
                {"path": "properties.addressPrefix", "propertyChangeType": "Modify", "children": None},
            ]},
        ]},
    ]
    _, signals = graph_from_whatif({"net": whatif_result(whatif_change("Modify", VNET, delta=delta))})
    assert signals.modified[VNET.lower()] == [
        PropertyChange("properties.virtualNetworkPeerings", "Delete"),
        PropertyChange("properties.subnets.0.properties.addressPrefix", "Modify"),
    ]


def test_failed_entry_points_are_recorded_and_skipped():
    graph, signals = graph_from_whatif({
        "main": {"error": "InvalidTemplate: boom"},
        "workflow": {"status": "Failed", "error": {"code": "X", "message": "denied"}},
        "net": whatif_result(whatif_change("Create", KV)),
    })
    assert signals.failed == {"main": "InvalidTemplate: boom", "workflow": "denied"}
    assert set(graph.nodes) == {KV.lower()}


def test_flatten_delta_handles_none():
    assert flatten_delta(None) == []
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_normalise.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'infradiag.normalise'`.

- [ ] **Step 4: Implement**

`src/infradiag/normalise.py`:

```python
"""Turn raw what-if and Resource Graph JSON into Graphs with the same shape."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .ids import normalise_id, parse_id
from .model import BICEP, UNSUPPORTED, Graph, Node

# Child resources Azure returns inline in their parent. Expanded into nodes on both sides so
# what-if (which may list them separately, and omits inline ids) lines up with Resource Graph.
INLINE_CHILDREN: dict[str, tuple[str, ...]] = {
    "microsoft.network/virtualnetworks": ("subnets", "virtualNetworkPeerings"),
}

NODE_CHANGE_TYPES = {"Create", "Modify", "NoChange", "NoEffect", "Deploy"}


@dataclass(frozen=True)
class PropertyChange:
    path: str
    change: str


@dataclass
class WhatIfSignals:
    ignored: dict[str, set[str]] = field(default_factory=dict)
    modified: dict[str, list[PropertyChange]] = field(default_factory=dict)
    change_types: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)


def expand_inline_children(graph: Graph, node: Node) -> None:
    for prop in INLINE_CHILDREN.get(node.type, ()):
        for child in node.properties.get(prop) or []:
            name = child.get("name")
            if not name:
                continue
            child_id = child.get("id") or f"{node.id}/{prop}/{name}"
            graph.add_node(Node.from_id(
                child_id,
                name=name,
                properties=child.get("properties") or {},
                sources=set(node.sources),
                entry_points=set(node.entry_points),
            ))


def flatten_delta(items: list | None, prefix: str = "") -> list[PropertyChange]:
    changes: list[PropertyChange] = []
    for item in items or []:
        path = f"{prefix}.{item['path']}" if prefix else item["path"]
        if item.get("children"):
            changes.extend(flatten_delta(item["children"], path))
        else:
            changes.append(PropertyChange(path, item["propertyChangeType"]))
    return changes


def _error_text(raw: dict) -> str | None:
    error = raw.get("error")
    if not error and raw.get("status") != "Failed":
        return None
    if isinstance(error, dict):
        return error.get("message") or json.dumps(error)
    return str(error or "what-if status Failed")


def graph_from_whatif(results: dict[str, dict]) -> tuple[Graph, WhatIfSignals]:
    graph, signals = Graph(), WhatIfSignals()
    for entry, raw in sorted(results.items()):
        if (error := _error_text(raw)) is not None:
            signals.failed[entry] = error
            continue
        for change in raw.get("changes") or []:
            resource_id, change_type = change.get("resourceId", ""), change.get("changeType")
            if parse_id(resource_id) is None:
                continue
            nid = normalise_id(resource_id)
            if change_type == "Ignore":
                signals.ignored.setdefault(nid, set()).add(entry)
                continue
            if change_type == "Unsupported":
                graph.add_node(Node.from_id(resource_id, sources={BICEP}, entry_points={entry},
                                            flags={UNSUPPORTED}))
                signals.change_types.setdefault(nid, change_type)
                continue
            if change_type not in NODE_CHANGE_TYPES:
                continue
            after = change.get("after") or {}
            node = graph.add_node(Node.from_id(
                resource_id,
                name=after.get("name"),
                properties=after.get("properties") or {},
                sources={BICEP},
                entry_points={entry},
            ))
            expand_inline_children(graph, node)
            signals.change_types.setdefault(nid, change_type)
            if change_type == "Modify":
                signals.modified.setdefault(nid, []).extend(flatten_delta(change.get("delta")))
    return graph, signals
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_normalise.py -v`

Expected: 9 passed.

- [ ] **Step 6: Commit**

```bash
git add src/infradiag/normalise.py tests/factories.py tests/test_normalise.py
git commit -m "Normalise what-if output into a graph

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Normalise Resource Graph output

**Files:**
- Modify: `src/infradiag/normalise.py` (add `graph_from_arg`)
- Modify: `tests/factories.py` (append `arg_row`)
- Test: `tests/test_normalise.py` (append)

**Interfaces:**
- Consumes:
  - `expand_inline_children`, from Task 4
  - `model.AZURE`
- Produces:
  - `graph_from_arg(rows: list[dict]) -> Graph`
  - Each `rows` entry has `id`, `name`, `type`, `properties` and `managedBy`.

- [ ] **Step 1: Append to `tests/factories.py`**

```python
def arg_row(resource_id: str, name: str | None = None, properties: dict | None = None,
            managed_by: str = "") -> dict:
    return {"id": resource_id, "name": name or resource_id.rsplit("/", 1)[-1],
            "type": "", "properties": properties or {}, "managedBy": managed_by}
```

- [ ] **Step 2: Append failing tests to `tests/test_normalise.py`**

```python
from infradiag.normalise import graph_from_arg
from tests.factories import arg_row


def test_arg_rows_become_azure_nodes_with_managed_by():
    nic = rid(DEV, "Microsoft.Network/networkInterfaces/pe-kv.nic.1")
    graph = graph_from_arg([arg_row(KV), arg_row(nic, managed_by="/subscriptions/x/pe")])
    assert graph.nodes[KV.lower()].sources == {AZURE}
    assert graph.nodes[KV.lower()].managed_by is None
    assert graph.nodes[nic.lower()].managed_by == "/subscriptions/x/pe"


def test_arg_inline_subnets_keep_their_ids():
    subnet_id = VNET + "/subnets/snet-pe"
    graph = graph_from_arg([arg_row(VNET, properties={
        "subnets": [{"id": subnet_id, "name": "snet-pe", "properties": {"privateEndpoints": []}}],
    })])
    assert graph.nodes[subnet_id.lower()].sources == {AZURE}


def test_whatif_and_arg_ids_match_despite_case():
    lower_vnet = VNET.lower().replace("/resourcegroups/", "/resourceGroups/")
    bicep, _ = graph_from_whatif({"net": whatif_result(whatif_change("NoChange", VNET, properties={
        "subnets": [{"name": "snet-aca", "properties": {}}],
    }))})
    azure = graph_from_arg([arg_row(lower_vnet, properties={
        "subnets": [{"id": lower_vnet + "/subnets/SNET-ACA", "name": "snet-aca", "properties": {}}],
    })])
    assert set(bicep.nodes) == set(azure.nodes)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_normalise.py -v`

Expected: FAIL — `ImportError: cannot import name 'graph_from_arg'`.

- [ ] **Step 4: Implement — append to `src/infradiag/normalise.py`**

Change the model import line to `from .model import AZURE, BICEP, UNSUPPORTED, Graph, Node`, then append:

```python
def graph_from_arg(rows: list[dict]) -> Graph:
    graph = Graph()
    for row in rows:
        resource_id = row.get("id", "")
        if parse_id(resource_id) is None:
            continue
        node = graph.add_node(Node.from_id(
            resource_id,
            name=row.get("name"),
            properties=row.get("properties") or {},
            sources={AZURE},
            managed_by=row.get("managedBy") or None,
        ))
        expand_inline_children(graph, node)
    return graph
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_normalise.py -v`

Expected: 12 passed.

- [ ] **Step 6: Commit**

```bash
git add src/infradiag/normalise.py tests/factories.py tests/test_normalise.py
git commit -m "Normalise Resource Graph rows into a graph

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Edge passes (ID references and endpoints)

**Files:**
- Create: `src/infradiag/edges.py`
- Test: `tests/test_edges.py`

**Interfaces:**
- Consumes:
  - `ids.find_ids`, `resolve`
  - `model.Edge`, `Graph`, `Node`, `REF`, `ENDPOINT`, `STUB`
  - `normalise.INLINE_CHILDREN`
- Produces:
  - `get_path(obj: dict, dotted: str)` returns the nested value, or `None`.
  - `add_reference_edges(graph: Graph) -> None` adds `REF` edges labelled with a JSON path (e.g. `properties.ipConfigurations[].properties.subnet.id`). It also creates `STUB` nodes for targets outside the graph.
  - `add_endpoint_edges(graph: Graph) -> None` adds `ENDPOINT` edges, labelled `"endpoint"`, from container apps and jobs to the services whose hostname or key appears in their properties.

- [ ] **Step 1: Write the failing tests**

`tests/test_edges.py`:

```python
from infradiag.edges import add_endpoint_edges, add_reference_edges, get_path
from infradiag.model import ENDPOINT, REF, STUB, Edge, Graph
from tests.factories import DEV, SHARED, node, rid

VNET = rid(DEV, "Microsoft.Network/virtualNetworks/vnet-app-dev-weu")
SUBNET = VNET + "/subnets/snet-pe"
NSG = rid(DEV, "Microsoft.Network/networkSecurityGroups/nsg-1")
KV = rid(DEV, "Microsoft.KeyVault/vaults/kv-app-dev-weu")
PE = rid(DEV, "Microsoft.Network/privateEndpoints/pe-kv-app-dev-weu")
APP = rid(DEV, "Microsoft.App/containerApps/ca-api-dev-weu")
APPCFG = rid(DEV, "Microsoft.AppConfiguration/configurationStores/appcfg-app-dev-weu")
ACR = rid(SHARED, "Microsoft.ContainerRegistry/registries/acrappsharedweu")
APPI = rid(DEV, "Microsoft.Insights/components/appi-app-dev-weu")


def graph_of(*nodes):
    g = Graph()
    for n in nodes:
        g.add_node(n)
    return g


def test_get_path():
    assert get_path({"a": {"b": "x"}}, "a.b") == "x"
    assert get_path({"a": "x"}, "a.b") is None


def test_private_endpoint_edges_to_subnet_and_target():
    pe = node(PE, properties={
        "subnet": {"id": SUBNET},
        "privateLinkServiceConnections": [{"properties": {"privateLinkServiceId": KV}}],
    })
    g = graph_of(pe, node(SUBNET), node(KV))
    add_reference_edges(g)
    assert g.edges == {
        Edge(PE.lower(), SUBNET.lower(), REF, "properties.subnet.id"),
        Edge(PE.lower(), KV.lower(), REF,
             "properties.privateLinkServiceConnections[].properties.privateLinkServiceId"),
    }


def test_self_parent_and_child_references_are_dropped():
    vnet = node(VNET, properties={"self": VNET, "subnets": [{"id": SUBNET}], "other": {"id": SUBNET}})
    subnet = node(SUBNET, properties={"parent": VNET, "networkSecurityGroup": {"id": NSG}})
    g = graph_of(vnet, subnet, node(NSG))
    add_reference_edges(g)
    assert g.edges == {Edge(SUBNET.lower(), NSG.lower(), REF, "properties.networkSecurityGroup.id")}


def test_runtime_back_references_are_ignored():
    subnet = node(SUBNET, properties={
        "privateEndpoints": [{"id": PE}],
        "ipConfigurations": [{"id": PE + "/ipconfig"}],
    })
    g = graph_of(subnet, node(PE))
    add_reference_edges(g)
    assert g.edges == set()


def test_unevaluated_whatif_expressions_are_skipped():
    pe = node(PE, properties={"subnet": {"id": f"[reference('{SUBNET}').id]"}})
    g = graph_of(pe, node(SUBNET))
    add_reference_edges(g)
    assert g.edges == set()


def test_targets_outside_graph_become_stubs():
    platform = rid("rg-app-platform-weu", "Microsoft.Network/privateDnsZones/zone.example")
    pe = node(PE, properties={"dns": {"id": platform + "/A/rec"}})
    g = graph_of(pe)
    add_reference_edges(g)
    assert g.nodes[platform.lower()].flags == {STUB}
    assert g.edges == {Edge(PE.lower(), platform.lower(), REF, "properties.dns.id")}


def test_endpoint_edges_from_container_apps():
    app = node(APP, properties={
        "template": {"containers": [{
            "image": "acrappsharedweu.azurecr.io/api:1.2.3",
            "env": [
                {"name": "AppConfiguration__Endpoint", "value": "https://APPCFG-app-dev-weu.azconfig.io"},
                {"name": "APPLICATIONINSIGHTS_CONNECTION_STRING",
                 "value": "InstrumentationKey=09129bc7-0b1c;IngestionEndpoint=https://x/"},
            ],
        }]},
        "configuration": {"secrets": [{"keyVaultUrl": "https://kv-app-dev-weu.vault.azure.net/secrets/s"}]},
    })
    g = graph_of(
        app,
        node(APPCFG, properties={"endpoint": "https://appcfg-app-dev-weu.azconfig.io"}),
        node(KV, properties={"vaultUri": "https://kv-app-dev-weu.vault.azure.net/"}),
        node(ACR, properties={"loginServer": "acrappsharedweu.azurecr.io"}),
        node(APPI, properties={"InstrumentationKey": "09129bc7-0b1c"}),
    )
    add_endpoint_edges(g)
    assert {(e.target, e.kind) for e in g.edges} == {
        (APPCFG.lower(), ENDPOINT), (KV.lower(), ENDPOINT), (ACR.lower(), ENDPOINT), (APPI.lower(), ENDPOINT),
    }


def test_endpoint_edges_only_from_consumers():
    other = node(PE, properties={"note": "https://kv-app-dev-weu.vault.azure.net/"})
    g = graph_of(other, node(KV, properties={"vaultUri": "https://kv-app-dev-weu.vault.azure.net/"}))
    add_endpoint_edges(g)
    assert g.edges == set()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_edges.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'infradiag.edges'`.

- [ ] **Step 3: Implement**

`src/infradiag/edges.py`:

```python
"""Derive edges: ARM ID references (pass 1) and service endpoint references (pass 2)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from fnmatch import fnmatchcase
from urllib.parse import urlparse

from .ids import find_ids, resolve
from .model import ENDPOINT, REF, STUB, Edge, Graph, Node
from .normalise import INLINE_CHILDREN

# Properties Azure fills in at runtime. They point back at dependants (subnet -> NICs, SQL -> PEs)
# or are computed, so they never appear in what-if output and would only add reverse-direction noise.
RUNTIME_REF_PATHS = (
    "properties.ipConfigurations[].id",
    "properties.privateEndpoints[].id",
    "properties.serviceAssociationLinks*",
    "properties.resourceNavigationLinks*",
    "properties.networkInterfaces[].id",
    "properties.privateEndpointConnections*",
    "properties.subnets[].id",
    "properties.remoteGateways*",
)

# Where each service publishes the hostname/key that consumers put in their config (under properties).
ENDPOINT_PROPERTIES: dict[str, tuple[str, ...]] = {
    "microsoft.keyvault/vaults": ("vaultUri",),
    "microsoft.appconfiguration/configurationstores": ("endpoint",),
    "microsoft.sql/servers": ("fullyQualifiedDomainName",),
    "microsoft.storage/storageaccounts": (
        "primaryEndpoints.blob", "primaryEndpoints.queue", "primaryEndpoints.table", "primaryEndpoints.file",
    ),
    "microsoft.eventhub/namespaces": ("serviceBusEndpoint",),
    "microsoft.servicebus/namespaces": ("serviceBusEndpoint",),
    "microsoft.cognitiveservices/accounts": ("endpoint",),
    "microsoft.containerregistry/registries": ("loginServer",),
    "microsoft.insights/components": ("InstrumentationKey",),
}

CONSUMER_TYPES = ("microsoft.app/containerapps", "microsoft.app/jobs")

_UNEVALUATED = re.compile(r"^\[.*\]$", re.DOTALL)


def get_path(obj, dotted: str):
    current = obj
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _walk_strings(value, path: str, skip_top: tuple[str, ...] = ()) -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            if path == "properties" and key in skip_top:
                continue
            yield from _walk_strings(child, f"{path}.{key}", skip_top)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child, f"{path}[]", skip_top)
    elif isinstance(value, str) and not _UNEVALUATED.match(value):
        yield path, value


def add_reference_edges(graph: Graph) -> None:
    known = set(graph.nodes)
    for node in list(graph.nodes.values()):
        for path, text in _walk_strings(node.properties, "properties", INLINE_CHILDREN.get(node.type, ())):
            if any(fnmatchcase(path, pattern) for pattern in RUNTIME_REF_PATHS):
                continue
            for found in find_ids(text):
                target = resolve(found, known)
                if target == node.id or target.startswith(node.id + "/") or node.id.startswith(target + "/"):
                    continue
                if target not in graph.nodes:
                    graph.add_node(Node.from_id(target, flags={STUB}))
                    known.add(target)
                graph.edges.add(Edge(node.id, target, REF, path))


def _token(value: str) -> str | None:
    value = value.strip().lower()
    if "://" in value:
        return urlparse(value).hostname
    return value.split(":")[0].rstrip("/") or None


def add_endpoint_edges(graph: Graph) -> None:
    tokens: dict[str, str] = {}
    for node in graph.nodes.values():
        for prop in ENDPOINT_PROPERTIES.get(node.type, ()):
            value = get_path(node.properties, prop)
            if isinstance(value, str) and (token := _token(value)):
                tokens[token] = node.id
    for node in graph.nodes.values():
        if node.type not in CONSUMER_TYPES:
            continue
        for _, text in _walk_strings(node.properties, "properties"):
            lowered = text.lower()
            for token, target in tokens.items():
                if target != node.id and token in lowered:
                    graph.edges.add(Edge(node.id, target, ENDPOINT, "endpoint"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_edges.py -v`

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/infradiag/edges.py tests/test_edges.py
git commit -m "Derive reference and endpoint edges

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: az wrapper and extractors

**Files:**
- Create: `src/infradiag/azcli.py`
- Create: `src/infradiag/extract_bicep.py`
- Create: `src/infradiag/extract_azure.py`
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes:
  - `config.Config`, `EntryPoint`
  - `cache.write_json`, `read_json`, `CacheMissing`
- Produces:
  - `azcli.AzError(Exception)`
  - `azcli.run_az(args: list[str], cwd: Path | None = None, timeout: int = 900)`. It appends `--only-show-errors -o json`, returns the parsed JSON (or `None` for empty output), and raises `AzError` on a non-zero exit, a timeout, or a missing `az`.
  - `extract_bicep.whatif_args(cfg: Config, ep: EntryPoint) -> list[str]`
  - `extract_bicep.extract_bicep(cfg, run=None, log=print) -> dict[str, str]` writes `cache/whatif/<name>.json` for every entry point (`{"error": msg}` on failure) and returns entry point → error for the failures only.
  - `extract_bicep.load_whatif_cache(cfg) -> dict[str, dict]`
  - `extract_azure.build_query(cfg) -> str`
  - `extract_azure.extract_azure(cfg, run=None) -> int` writes `cache/arg.json` as `{"resource_groups": [...], "rows": [...]}` and returns the row count.
  - `extract_azure.load_arg_cache(cfg) -> list[dict]` raises `CacheMissing` if the cache is missing or covers fewer RGs than the config.

- [ ] **Step 1: Write the failing tests**

`tests/test_extract.py`:

```python
import json
import subprocess
from pathlib import Path

import pytest

from infradiag import azcli
from infradiag.azcli import AzError, run_az
from infradiag.cache import CacheMissing
from infradiag.config import Config, EntryPoint
from infradiag.extract_azure import build_query, extract_azure, load_arg_cache
from infradiag.extract_bicep import extract_bicep, load_whatif_cache, whatif_args

SUB = "0a1b2c3d-0000-4000-8000-000000000001"


def make_cfg(tmp_path: Path) -> Config:
    eps = (
        EntryPoint("network-dev", "infra-network-dev", "infra/network.bicep",
                   ("environmentName=dev",), ("rg-app-dev-weu",)),
        EntryPoint("main-dev", "infra-dev", "infra/main.bicep",
                   ("infra/env/dev.bicepparam", "apiImageTag=1.2.3"), ("rg-app-dev-weu",)),
    )
    return Config(tmp_path / "bicep-project", SUB, "westeurope",
                  ("rg-app-dev-weu", "rg-app-shared-weu"), eps, tmp_path / "cache", tmp_path / "out")


def test_run_az_parses_json_and_appends_output_flags(monkeypatch):
    calls = []
    monkeypatch.setattr(azcli.shutil, "which", lambda _: "/usr/bin/az")

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs["cwd"]))
        return subprocess.CompletedProcess(cmd, 0, stdout='{"ok": true}', stderr="WARNING: x")

    monkeypatch.setattr(azcli.subprocess, "run", fake_run)
    assert run_az(["graph", "query"], cwd=Path("/tmp")) == {"ok": True}
    assert calls == [(["/usr/bin/az", "graph", "query", "--only-show-errors", "-o", "json"], Path("/tmp"))]


def test_run_az_raises_with_stderr(monkeypatch):
    monkeypatch.setattr(azcli.shutil, "which", lambda _: "/usr/bin/az")
    monkeypatch.setattr(azcli.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="ERROR: denied\n"))
    with pytest.raises(AzError, match="ERROR: denied"):
        run_az(["x"])


def test_run_az_missing_cli(monkeypatch):
    monkeypatch.setattr(azcli.shutil, "which", lambda _: None)
    with pytest.raises(AzError, match="az CLI not found"):
        run_az(["x"])


def test_whatif_args_mirror_pipeline(tmp_path):
    cfg = make_cfg(tmp_path)
    assert whatif_args(cfg, cfg.entry_points[1]) == [
        "deployment", "sub", "what-if",
        "--subscription", SUB,
        "--name", "infra-dev",
        "--location", "westeurope",
        "--template-file", "infra/main.bicep",
        "--result-format", "FullResourcePayloads",
        "--no-pretty-print",
        "--parameters", "infra/env/dev.bicepparam",
        "--parameters", "apiImageTag=1.2.3",
    ]


def test_extract_bicep_continues_after_failure_and_caches_error(tmp_path):
    cfg = make_cfg(tmp_path)
    seen = []

    def fake_run(args, cwd=None):
        seen.append((args[args.index("--name") + 1], cwd))
        if "infra-network-dev" in args:
            raise AzError("InvalidTemplate")
        return {"status": "Succeeded", "error": None, "changes": []}

    failures = extract_bicep(cfg, run=fake_run, log=lambda *_: None)
    assert failures == {"network-dev": "InvalidTemplate"}
    assert seen == [("infra-network-dev", cfg.bicep_root), ("infra-dev", cfg.bicep_root)]
    cached = load_whatif_cache(cfg)
    assert cached["network-dev"] == {"error": "InvalidTemplate"}
    assert cached["main-dev"]["status"] == "Succeeded"


def test_load_whatif_cache_names_missing_entry_points(tmp_path):
    with pytest.raises(CacheMissing, match="network-dev, main-dev .*infradiag extract --bicep"):
        load_whatif_cache(make_cfg(tmp_path))


def test_build_query_scopes_subscription_and_rgs(tmp_path):
    q = build_query(make_cfg(tmp_path))
    assert f"subscriptionId =~ '{SUB}'" in q
    assert "resourceGroup in~ ('rg-app-dev-weu', 'rg-app-shared-weu')" in q
    assert "managedBy" in q and "order by id asc" in q


def test_extract_azure_follows_skip_token(tmp_path):
    cfg = make_cfg(tmp_path)
    pages = [
        {"data": [{"id": "a"}], "skip_token": "tok1"},
        {"data": [{"id": "b"}], "skip_token": None},
    ]
    calls = []

    def fake_run(args, cwd=None):
        calls.append(args)
        return pages[len(calls) - 1]

    assert extract_azure(cfg, run=fake_run) == 2
    assert "--skip-token" not in calls[0]
    assert calls[1][calls[1].index("--skip-token") + 1] == "tok1"
    assert load_arg_cache(cfg) == [{"id": "a"}, {"id": "b"}]


def test_load_arg_cache_rejects_stale_rg_set(tmp_path):
    cfg = make_cfg(tmp_path)
    (cfg.cache_dir).mkdir(parents=True)
    (cfg.cache_dir / "arg.json").write_text(json.dumps({"resource_groups": ["rg-app-dev-weu"], "rows": []}))
    with pytest.raises(CacheMissing, match="rg-app-shared-weu"):
        load_arg_cache(cfg)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_extract.py -v`

Expected: FAIL — `ImportError` / `ModuleNotFoundError` for `infradiag.azcli`.

- [ ] **Step 3: Implement**

`src/infradiag/azcli.py`:

```python
"""Thin wrapper over the az CLI so we reuse the user's existing az login."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class AzError(Exception):
    pass


def run_az(args: list[str], cwd: Path | None = None, timeout: int = 900):
    exe = shutil.which("az")
    if exe is None:
        raise AzError("az CLI not found on PATH")
    try:
        proc = subprocess.run(
            [exe, *args, "--only-show-errors", "-o", "json"],
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise AzError(f"az {' '.join(args[:3])} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise AzError(proc.stderr.strip() or f"az exited with {proc.returncode}")
    out = proc.stdout.strip()
    return json.loads(out) if out else None
```

`src/infradiag/extract_bicep.py`:

```python
"""Run what-if for each Bicep entry point and cache the raw result."""

from __future__ import annotations

from .azcli import AzError, run_az
from .cache import CacheMissing, read_json, write_json
from .config import Config, EntryPoint


def whatif_args(cfg: Config, ep: EntryPoint) -> list[str]:
    args = [
        "deployment", "sub", "what-if",
        "--subscription", cfg.subscription,
        "--name", ep.deployment_name,
        "--location", cfg.location,
        "--template-file", ep.template,
        "--result-format", "FullResourcePayloads",
        "--no-pretty-print",
    ]
    for param in ep.parameters:
        args += ["--parameters", param]
    return args


def _cache_path(cfg: Config, ep: EntryPoint):
    return cfg.cache_dir / "whatif" / f"{ep.name}.json"


def extract_bicep(cfg: Config, run=None, log=print) -> dict[str, str]:
    run = run or run_az
    failures: dict[str, str] = {}
    for ep in cfg.entry_points:
        log(f"what-if {ep.name} ({ep.template}) ...")
        try:
            raw = run(whatif_args(cfg, ep), cwd=cfg.bicep_root) or {}
        except AzError as exc:
            raw = {"error": str(exc)}
        if raw.get("error"):
            failures[ep.name] = raw["error"] if isinstance(raw["error"], str) else str(raw["error"])
            log(f"  FAILED: {failures[ep.name]}")
        write_json(_cache_path(cfg, ep), raw)
    return failures


def load_whatif_cache(cfg: Config) -> dict[str, dict]:
    missing = [ep.name for ep in cfg.entry_points if not _cache_path(cfg, ep).exists()]
    if missing:
        raise CacheMissing(f"no what-if cache for: {', '.join(missing)} — run `infradiag extract --bicep`")
    return {ep.name: read_json(_cache_path(cfg, ep), "infradiag extract --bicep") for ep in cfg.entry_points}
```

`src/infradiag/extract_azure.py`:

```python
"""Query Azure Resource Graph for everything in the configured RGs and cache the rows."""

from __future__ import annotations

from .azcli import run_az
from .cache import CacheMissing, read_json, write_json
from .config import Config

HINT = "infradiag extract --azure"


def build_query(cfg: Config) -> str:
    rgs = ", ".join(f"'{rg}'" for rg in cfg.resource_groups)
    return (
        "resources"
        f" | where subscriptionId =~ '{cfg.subscription}' and resourceGroup in~ ({rgs})"
        " | project id, name, type, resourceGroup, subscriptionId, location, kind, managedBy, sku, identity, properties"
        " | order by id asc"
    )


def extract_azure(cfg: Config, run=None) -> int:
    run = run or run_az
    rows: list[dict] = []
    skip_token = None
    while True:
        args = ["graph", "query", "-q", build_query(cfg), "--first", "1000", "--subscriptions", cfg.subscription]
        if skip_token:
            args += ["--skip-token", skip_token]
        page = run(args) or {}
        rows.extend(page.get("data") or [])
        skip_token = page.get("skip_token")
        if not skip_token:
            break
    write_json(cfg.cache_dir / "arg.json", {"resource_groups": list(cfg.resource_groups), "rows": rows})
    return len(rows)


def load_arg_cache(cfg: Config) -> list[dict]:
    cached = read_json(cfg.cache_dir / "arg.json", HINT)
    missing = sorted(set(cfg.resource_groups) - set(cached.get("resource_groups") or []))
    if missing:
        raise CacheMissing(f"Resource Graph cache lacks {', '.join(missing)} — run `{HINT}`")
    return cached.get("rows") or []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_extract.py -v`

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add src/infradiag/azcli.py src/infradiag/extract_bicep.py src/infradiag/extract_azure.py tests/test_extract.py
git commit -m "Add az wrapper, what-if and Resource Graph extractors

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Rules file and drift report

**Files:**
- Create: `views.yaml`
- Create: `src/infradiag/rules.py`
- Create: `src/infradiag/diff.py`
- Test: `tests/test_diff.py`

**Interfaces:**
- Consumes:
  - `model.*`
  - `normalise.WhatIfSignals`, `PropertyChange`
  - `config.Config`
  - `ids.parse_id`
- Produces:
  - `rules.matches(value: str, patterns) -> bool` — case-insensitive `fnmatch`.
  - `@dataclass(frozen=True) rules.CollapseRule(type: str, target_path: str, label: str)`
  - `@dataclass(frozen=True) rules.NameCollapse(pattern: str, label: str)`
  - `@dataclass(frozen=True) rules.Rules`. Every field has a default, so `Rules()` is valid:
    - `hide_types: tuple[str, ...] = ()`
    - `hide_managed: bool = False`
    - `collapse: tuple[CollapseRule, ...] = ()`
    - `icons: dict[str, str] = {}`
    - `implicit_types: tuple[str, ...] = ()`
    - `ignore_modify_paths: tuple[str, ...] = ()`
    - `summary_groups: tuple[tuple[str, tuple[str, ...]], ...] = ()`
    - `summary_name_overrides: tuple[tuple[str, str], ...] = ()`
    - `summary_hide_types: tuple[str, ...] = ()`
    - `summary_nest_into_types: tuple[str, ...] = ()`
    - `summary_collapse_names: tuple[NameCollapse, ...] = ()`
  - `rules.load_rules(path: Path) -> Rules`
  - `@dataclass(frozen=True) diff.DriftItem(id, type, name, resource_group, note="")`
  - `@dataclass diff.DriftReport`, with fields:
    - `matched: int`
    - `azure_only`, `unverified`, `expected_implicit`, `bicep_only`, `not_indexed`, `out_of_scope`: `list[DriftItem]`
    - `modified: dict[str, list[PropertyChange]]`
    - `edges_bicep_only: list[Edge]`, `edges_azure_only: list[Edge]`
    - `failed: dict[str, str]`
  - `diff.compute_drift(bicep: Graph, azure: Graph, signals: WhatIfSignals, rules: Rules, cfg: Config) -> DriftReport`
  - `diff.render_markdown(report: DriftReport, names: dict[str, str]) -> str` — `names` maps node id → display name.

- [ ] **Step 1: Create `views.yaml`**

```yaml
# Rules for views and the drift report. Types are lowercased ARM types; patterns use fnmatch (* ?).

# --- shared by both diagrams ---------------------------------------------------------------
hide_types:
  - microsoft.authorization/*
  - microsoft.insights/diagnosticsettings
  - microsoft.managedidentity/userassignedidentities/federatedidentitycredentials
  - microsoft.sql/servers/firewallrules
  - microsoft.network/privatednszones/a
  - microsoft.network/privatednszones/cname
  - microsoft.network/privatednszones/soa
  - microsoft.network/privateendpoints/privatednszonegroups
  - microsoft.network/networkinterfaces       # folded into their private endpoint
  - microsoft.app/containerapps/revisions
  - microsoft.portal/dashboards
hide_managed: true                             # resources Azure creates for another (managedBy set)

collapse:                                      # node -> edge between its parent and a referenced resource
  - type: microsoft.network/privatednszones/virtualnetworklinks
    target_path: virtualNetwork.id
    label: dns link
  - type: microsoft.network/virtualnetworks/virtualnetworkpeerings
    target_path: remoteVirtualNetwork.id
    label: peering

# --- drift report ----------------------------------------------------------------------------
implicit_types:                                # Azure-only resources that are expected, not drift
  - microsoft.network/networkinterfaces
  - microsoft.portal/dashboards                # App Insights' auto-created dashboard
ignore_modify_paths:
  - properties.virtualNetworkPeerings*         # deployed by peering.bicep as child resources
  - properties.template.containers.*.image     # image tags move with every code deploy

# --- icons (paths on https://icons.terrastruct.com/, verified to exist) -----------------------
icons:
  _subscription: azure/General Service Icons/Subscriptions.svg
  _resourcegroup: azure/General Service Icons/Resource Groups.svg
  microsoft.network/virtualnetworks: azure/Networking Service Color/Virtual Networks.svg
  microsoft.network/virtualnetworkgateways: azure/Networking Service Color/Virtual Network Gateways.svg
  microsoft.network/privatednszones: azure/Networking Service Color/DNS Private Zones.svg
  microsoft.network/networksecuritygroups: azure/Networking Service Color/Network Security Groups (Classic).svg
  microsoft.network/publicipaddresses: azure/Networking Service Color/Public IP Addresses.svg
  microsoft.sql/servers: azure/Databases Service Color/SQL Servers.svg
  microsoft.sql/servers/databases: azure/Databases Service Color/SQL Databases.svg
  microsoft.dbforpostgresql/flexibleservers: azure/Databases Service Color/Azure Database for PostgreSQL servers.svg
  microsoft.keyvault/vaults: azure/Security Service Color/Key Vaults.svg
  microsoft.storage/storageaccounts: azure/Storage Service Color/Storage Accounts.svg
  microsoft.containerregistry/registries: azure/Container Service Color/Container Registries.svg
  microsoft.containerservice/managedclusters: azure/Container Service Color/Kubernetes Services.svg
  microsoft.app/containerapps: azure/Container Service Color/Container Instances.svg   # no Container Apps icon in set
  microsoft.app/jobs: azure/Container Service Color/Container Instances.svg
  microsoft.operationalinsights/workspaces: azure/Analytics Service Color/Log Analytics Workspaces.svg
  microsoft.insights/components: azure/DevOps Service Color/Application Insights.svg
  microsoft.insights/privatelinkscopes: azure/Management and Governance Service Color/Monitor.svg
  microsoft.eventhub/namespaces: azure/Analytics Service Color/Event Hubs.svg
  microsoft.servicebus/namespaces: azure/Integration Service Color/Azure Service Bus.svg
  microsoft.appconfiguration/configurationstores: azure/Integration Service Color/App Configuration.svg
  microsoft.cognitiveservices/accounts: azure/AI and ML Service Color/Cognitive Services.svg
  microsoft.managedidentity/userassignedidentities: azure/Identity Service Color/Managed Identities.svg
  microsoft.devopsinfrastructure/pools: azure/DevOps Service Color/Azure Pipelines.svg

# --- summary diagram -------------------------------------------------------------------------
summary:
  groups:                                      # order = drawing order; unmatched types go to "Other"
    Connectivity: [microsoft.network/virtualnetworkgateways, microsoft.network/dnsresolvers]
    Networking: [microsoft.network/virtualnetworks, microsoft.network/natgateways, microsoft.network/publicipaddresses]
    Workloads:
      - microsoft.app/managedenvironments
      - microsoft.app/containerapps
      - microsoft.app/jobs
      - microsoft.containerservice/managedclusters
    Data & integration:
      - microsoft.sql/servers
      - microsoft.dbforpostgresql/flexibleservers
      - microsoft.storage/storageaccounts
      - microsoft.eventhub/namespaces
      - microsoft.servicebus/namespaces
      - microsoft.keyvault/vaults
      - microsoft.appconfiguration/configurationstores
      - microsoft.cognitiveservices/accounts
    Observability: [microsoft.operationalinsights/workspaces, microsoft.insights/components, microsoft.insights/privatelinkscopes]
    Platform:
      - microsoft.containerregistry/registries
      - microsoft.devopsinfrastructure/pools
  name_overrides:                              # name pattern -> group (checked before types)
    pip-app-vgw-*: Connectivity
    vnet-app-dev-*: Workloads
  hide_types:
    - microsoft.network/privateendpoints
    - microsoft.network/networksecuritygroups
    - microsoft.network/privatednszones
    - microsoft.managedidentity/userassignedidentities
  nest_into_types: [microsoft.app/managedenvironments]   # apps/jobs drawn inside their environment
  collapse_names:
    - pattern: ca-worker-*
      label: Workers
```

- [ ] **Step 2: Write the failing tests**

`tests/test_diff.py`:

```python
from pathlib import Path

from infradiag.config import Config, EntryPoint
from infradiag.diff import compute_drift, render_markdown
from infradiag.model import AZURE, BICEP, REF, Edge, Graph
from infradiag.normalise import PropertyChange, WhatIfSignals
from infradiag.rules import Rules, load_rules, matches
from tests.factories import DEV, SHARED, node, rid

KV = rid(DEV, "Microsoft.KeyVault/vaults/kv-app-dev-weu")
SQL = rid(DEV, "Microsoft.Sql/servers/sql-app-dev-weu")
WEB = rid(DEV, "Microsoft.App/containerApps/ca-web-dev-weu")
NIC = rid(DEV, "Microsoft.Network/networkInterfaces/pe-kv.nic.1")
DASH = rid(SHARED, "Microsoft.Portal/dashboards/dash-app-shared-weu")
HUB = rid(SHARED, "Microsoft.Network/virtualNetworks/vnet-app-shared-weu")
NEW = rid(DEV, "Microsoft.Storage/storageAccounts/stnew")
TOPIC = rid(SHARED, "Microsoft.ServiceBus/namespaces/sb/topics/t1")
ELSEWHERE = rid("rg-app-platform-weu", "Microsoft.Network/privateDnsZones/z")


def cfg(failed_rgs=("rg-app-dev-weu",)) -> Config:
    eps = (
        EntryPoint("main-dev", "infra-dev", "infra/main.bicep", (), tuple(failed_rgs)),
        EntryPoint("shared", "infra-shared", "infra/shared.bicep", (), ("rg-app-shared-weu",)),
    )
    return Config(Path("."), "sub", "westeurope", ("rg-app-dev-weu", "rg-app-shared-weu"),
                  eps, Path("cache"), Path("out"))


def graphs(bicep_nodes, azure_nodes):
    b, a = Graph(), Graph()
    for n in bicep_nodes:
        b.add_node(n)
    for n in azure_nodes:
        a.add_node(n)
    return b, a


RULES = Rules(implicit_types=("microsoft.portal/*",),
              ignore_modify_paths=("properties.virtualNetworkPeerings*",))


def test_matches_is_case_insensitive_glob():
    assert matches("Microsoft.Portal/dashboards", ("microsoft.portal/*",))
    assert not matches("microsoft.keyvault/vaults", ())


def test_classifies_azure_only_resources():
    b, a = graphs(
        [node(KV, sources=[BICEP])],
        [node(KV, sources=[AZURE]), node(WEB, sources=[AZURE]), node(NIC, sources=[AZURE], managed_by="pe"),
         node(DASH, sources=[AZURE])],
    )
    signals = WhatIfSignals(ignored={WEB.lower(): {"main-dev"}}, change_types={KV.lower(): "NoChange"})
    report = compute_drift(b, a, signals, RULES, cfg())
    assert report.matched == 1
    assert [(i.name, i.note) for i in report.azure_only] == [("ca-web-dev-weu", "confirmed by what-if Ignore")]
    assert sorted(i.name for i in report.expected_implicit) == ["dash-app-shared-weu", "pe-kv.nic.1"]


def test_failed_entry_point_makes_its_rgs_unverified():
    b, a = graphs([], [node(WEB, sources=[AZURE]), node(HUB, sources=[AZURE])])
    signals = WhatIfSignals(failed={"main-dev": "InvalidTemplate"})
    report = compute_drift(b, a, signals, RULES, cfg())
    assert [(i.name, i.note) for i in report.unverified] == [("ca-web-dev-weu", "main-dev what-if failed")]
    assert [i.name for i in report.azure_only] == ["vnet-app-shared-weu"]
    assert report.failed == {"main-dev": "InvalidTemplate"}


def test_bicep_only_split_by_whatif_change_type_and_scope():
    b, a = graphs([node(NEW, sources=[BICEP]), node(TOPIC, sources=[BICEP]), node(ELSEWHERE, sources=[BICEP])], [])
    signals = WhatIfSignals(change_types={NEW.lower(): "Create", TOPIC.lower(): "NoChange",
                                          ELSEWHERE.lower(): "NoChange"})
    report = compute_drift(b, a, signals, RULES, cfg())
    assert [i.name for i in report.bicep_only] == ["stnew"]
    assert [i.name for i in report.not_indexed] == ["t1"]
    assert [i.name for i in report.out_of_scope] == ["z"]


def test_modified_paths_filtered_by_ignore_rules():
    b, a = graphs([node(HUB, sources=[BICEP])], [node(HUB, sources=[AZURE])])
    signals = WhatIfSignals(modified={
        HUB.lower(): [PropertyChange("properties.virtualNetworkPeerings", "Delete"),
                      PropertyChange("properties.addressSpace.addressPrefixes.0", "Modify")],
        KV.lower(): [PropertyChange("properties.virtualNetworkPeerings", "Delete")],
    })
    report = compute_drift(b, a, signals, RULES, cfg())
    assert report.modified == {HUB.lower(): [PropertyChange("properties.addressSpace.addressPrefixes.0", "Modify")]}


def test_edge_differences_only_between_nodes_on_both_sides():
    b, a = graphs([node(KV, sources=[BICEP]), node(SQL, sources=[BICEP])],
                  [node(KV, sources=[AZURE]), node(SQL, sources=[AZURE]), node(WEB, sources=[AZURE])])
    b.edges.add(Edge(KV.lower(), SQL.lower(), REF, "properties.x.id"))
    a.edges.add(Edge(SQL.lower(), KV.lower(), REF, "properties.y.id"))
    a.edges.add(Edge(WEB.lower(), KV.lower(), REF, "properties.z.id"))
    report = compute_drift(b, a, WhatIfSignals(), RULES, cfg())
    assert [(e.source, e.target) for e in report.edges_bicep_only] == [(KV.lower(), SQL.lower())]
    assert [(e.source, e.target) for e in report.edges_azure_only] == [(SQL.lower(), KV.lower())]


def test_markdown_has_all_sections_and_failure_banner():
    b, a = graphs([node(NEW, sources=[BICEP])], [node(WEB, sources=[AZURE])])
    signals = WhatIfSignals(failed={"shared": "boom"}, change_types={NEW.lower(): "Create"})
    report = compute_drift(b, a, signals, RULES, cfg())
    md = render_markdown(report, {NEW.lower(): "stnew", WEB.lower(): "ca-web-dev-weu"})
    for heading in ("# Drift report", "## Summary", "## Failed entry points", "## In Azure, not in Bicep",
                    "## Unverified", "## Expected implicit", "## In Bicep, not in Azure",
                    "## Not indexed by Resource Graph", "## Outside query scope",
                    "## Configuration drift", "## Edge differences"):
        assert heading in md
    assert "`shared`: boom" in md
    assert "| ca-web-dev-weu |" in md


def test_load_rules_reads_project_views_yaml():
    rules = load_rules(Path(__file__).parent.parent / "views.yaml")
    assert rules.hide_managed is True
    assert rules.collapse[0].label == "dns link"
    assert rules.summary_groups[0][0] == "Connectivity"
    assert ("pip-app-vgw-*", "Connectivity") in rules.summary_name_overrides
    assert rules.summary_collapse_names[0].label == "Workers"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_diff.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'infradiag.diff'`.

- [ ] **Step 4: Implement**

`src/infradiag/rules.py`:

```python
"""Load views.yaml: filtering, grouping, icon and drift rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

import yaml


def matches(value: str, patterns) -> bool:
    value = value.lower()
    return any(fnmatchcase(value, p.lower()) for p in patterns)


@dataclass(frozen=True)
class CollapseRule:
    type: str
    target_path: str
    label: str


@dataclass(frozen=True)
class NameCollapse:
    pattern: str
    label: str


@dataclass(frozen=True)
class Rules:
    hide_types: tuple[str, ...] = ()
    hide_managed: bool = False
    collapse: tuple[CollapseRule, ...] = ()
    icons: dict[str, str] = field(default_factory=dict)
    implicit_types: tuple[str, ...] = ()
    ignore_modify_paths: tuple[str, ...] = ()
    summary_groups: tuple[tuple[str, tuple[str, ...]], ...] = ()
    summary_name_overrides: tuple[tuple[str, str], ...] = ()
    summary_hide_types: tuple[str, ...] = ()
    summary_nest_into_types: tuple[str, ...] = ()
    summary_collapse_names: tuple[NameCollapse, ...] = ()


def _lower(items) -> tuple[str, ...]:
    return tuple(str(i).lower() for i in items or [])


def load_rules(path: Path) -> Rules:
    data = yaml.safe_load(Path(path).read_text()) or {}
    summary = data.get("summary") or {}
    return Rules(
        hide_types=_lower(data.get("hide_types")),
        hide_managed=bool(data.get("hide_managed", False)),
        collapse=tuple(CollapseRule(c["type"].lower(), c["target_path"], c["label"])
                       for c in data.get("collapse") or []),
        icons={k.lower(): v for k, v in (data.get("icons") or {}).items()},
        implicit_types=_lower(data.get("implicit_types")),
        ignore_modify_paths=tuple(data.get("ignore_modify_paths") or []),
        summary_groups=tuple((name, _lower(types)) for name, types in (summary.get("groups") or {}).items()),
        summary_name_overrides=tuple((pattern, group) for pattern, group in (summary.get("name_overrides") or {}).items()),
        summary_hide_types=_lower(summary.get("hide_types")),
        summary_nest_into_types=_lower(summary.get("nest_into_types")),
        summary_collapse_names=tuple(NameCollapse(c["pattern"], c["label"])
                                     for c in summary.get("collapse_names") or []),
    )
```

`src/infradiag/diff.py`:

```python
"""Compare the Bicep (what-if) graph with the Azure (Resource Graph) graph."""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .model import REF, STUB, UNSUPPORTED, Edge, Graph, Node
from .normalise import PropertyChange, WhatIfSignals
from .rules import Rules, matches

# what-if compared these against live state, so the resource exists even if Resource Graph lacks it
EXISTS_PER_WHATIF = {"NoChange", "Modify", "NoEffect", "Deploy"}


@dataclass(frozen=True)
class DriftItem:
    id: str
    type: str
    name: str
    resource_group: str
    note: str = ""


@dataclass
class DriftReport:
    matched: int = 0
    azure_only: list[DriftItem] = field(default_factory=list)
    unverified: list[DriftItem] = field(default_factory=list)
    expected_implicit: list[DriftItem] = field(default_factory=list)
    bicep_only: list[DriftItem] = field(default_factory=list)
    not_indexed: list[DriftItem] = field(default_factory=list)
    out_of_scope: list[DriftItem] = field(default_factory=list)
    modified: dict[str, list[PropertyChange]] = field(default_factory=dict)
    edges_bicep_only: list[Edge] = field(default_factory=list)
    edges_azure_only: list[Edge] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


def _item(n: Node, note: str = "") -> DriftItem:
    return DriftItem(n.id, n.type, n.name, n.resource_group, note)


def _real(graph: Graph) -> dict[str, Node]:
    return {i: n for i, n in graph.nodes.items() if STUB not in n.flags}


def compute_drift(bicep: Graph, azure: Graph, signals: WhatIfSignals, rules: Rules, cfg: Config) -> DriftReport:
    report = DriftReport(failed=dict(signals.failed))
    b, a = _real(bicep), _real(azure)
    both = b.keys() & a.keys()
    report.matched = len(both)

    failed_by_rg: dict[str, list[str]] = {}
    for ep in cfg.entry_points:
        if ep.name in signals.failed:
            for rg in ep.resource_groups:
                failed_by_rg.setdefault(rg, []).append(ep.name)

    for rid in sorted(a.keys() - b.keys()):
        n = a[rid]
        if n.managed_by or matches(n.type, rules.implicit_types):
            report.expected_implicit.append(_item(n))
        elif n.resource_group in failed_by_rg:
            report.unverified.append(_item(n, f"{', '.join(failed_by_rg[n.resource_group])} what-if failed"))
        else:
            report.azure_only.append(_item(n, "confirmed by what-if Ignore" if rid in signals.ignored else ""))

    scope = set(cfg.resource_groups)
    for rid in sorted(b.keys() - a.keys()):
        n = b[rid]
        if n.resource_group not in scope:
            report.out_of_scope.append(_item(n))
        elif signals.change_types.get(rid) in EXISTS_PER_WHATIF:
            report.not_indexed.append(_item(n))
        else:
            report.bicep_only.append(_item(n, signals.change_types.get(rid, "")))

    for rid in sorted(signals.modified):
        kept = [c for c in signals.modified[rid] if not matches(c.path, rules.ignore_modify_paths)]
        if kept and rid in b:
            report.modified[rid] = kept

    def comparable(graph: Graph, nodes: dict[str, Node]) -> dict[tuple[str, str], Edge]:
        return {
            (e.source, e.target): e
            for e in sorted(graph.edges)
            if e.kind == REF and e.source in both and e.target in both and UNSUPPORTED not in nodes[e.source].flags
        }

    eb, ea = comparable(bicep, b), comparable(azure, a)
    report.edges_bicep_only = [eb[k] for k in sorted(eb.keys() - ea.keys())]
    report.edges_azure_only = [ea[k] for k in sorted(ea.keys() - eb.keys())]
    return report


def _table(items: list[DriftItem]) -> list[str]:
    if not items:
        return ["None.", ""]
    lines = ["| Resource | Type | Resource group | Note |", "|---|---|---|---|"]
    lines += [f"| {i.name} | `{i.type}` | {i.resource_group} | {i.note} |" for i in items]
    return lines + [""]


def _edges(edges: list[Edge], names: dict[str, str]) -> list[str]:
    if not edges:
        return ["None.", ""]
    return [f"- {names.get(e.source, e.source)} → {names.get(e.target, e.target)} (`{e.label}`)" for e in edges] + [""]


def render_markdown(report: DriftReport, names: dict[str, str]) -> str:
    lines = [
        "# Drift report",
        "",
        "_Bicep (what-if) compared with Azure (Resource Graph)._",
        "",
        "## Summary",
        "",
        "| Category | Count |",
        "|---|---|",
        f"| Matched | {report.matched} |",
        f"| In Azure, not in Bicep | {len(report.azure_only)} |",
        f"| Unverified (entry point failed) | {len(report.unverified)} |",
        f"| Expected implicit | {len(report.expected_implicit)} |",
        f"| In Bicep, not in Azure | {len(report.bicep_only)} |",
        f"| Not indexed by Resource Graph | {len(report.not_indexed)} |",
        f"| Outside query scope | {len(report.out_of_scope)} |",
        f"| Configuration drift | {len(report.modified)} |",
        f"| Edge differences | {len(report.edges_bicep_only) + len(report.edges_azure_only)} |",
        "",
    ]
    if report.failed:
        lines += ["## Failed entry points", "",
                  "> Bicep data for these entry points is missing; resources in their resource groups are"
                  " listed as Unverified instead of drift.", ""]
        lines += [f"- `{name}`: {error}" for name, error in sorted(report.failed.items())] + [""]
    else:
        lines += ["## Failed entry points", "", "None.", ""]
    lines += ["## In Azure, not in Bicep", ""] + _table(report.azure_only)
    lines += ["## Unverified", ""] + _table(report.unverified)
    lines += ["## Expected implicit", "", "_Created by Azure for another resource; not drift._", ""]
    lines += _table(report.expected_implicit)
    lines += ["## In Bicep, not in Azure", "", "_The next deployment would create these._", ""]
    lines += _table(report.bicep_only)
    lines += ["## Not indexed by Resource Graph", "",
              "_what-if says these exist, but Resource Graph's `resources` table doesn't list their type._", ""]
    lines += _table(report.not_indexed)
    lines += ["## Outside query scope", ""] + _table(report.out_of_scope)
    lines += ["## Configuration drift", "", "_Properties the next deployment would change (what-if `Modify`)._", ""]
    if not report.modified:
        lines += ["None.", ""]
    for rid, changes in report.modified.items():
        lines += [f"### {names.get(rid, rid)}", ""]
        lines += [f"- `{c.path}` — {c.change}" for c in changes] + [""]
    lines += ["## Edge differences", "", "### Only in Bicep", ""] + _edges(report.edges_bicep_only, names)
    lines += ["### Only in Azure", ""] + _edges(report.edges_azure_only, names)
    return "\n".join(lines)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_diff.py -v`

Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add views.yaml src/infradiag/rules.py src/infradiag/diff.py tests/test_diff.py
git commit -m "Add view rules and drift report

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Common filtering and the detailed view

**Files:**
- Create: `src/infradiag/views.py`
- Test: `tests/test_views.py`

**Interfaces:**
- Consumes:
  - `model.*`
  - `rules.Rules`, `rules.matches`
  - `edges.get_path`
  - `ids.resolve`
- Produces:
  - `@dataclass ViewNode(key: str, label: str, sublabel: str = "", icon: str | None = None, style: str = "normal", children: list[ViewNode] = [])`. `style` is one of `normal|bicep_only|azure_only|stub`.
  - `@dataclass(frozen=True, order=True) ViewEdge(source: str, target: str, label: str = "", style: str = "solid")`. `style` is one of `solid|dashed|private`. `source` and `target` are `ViewNode.key`s.
  - `@dataclass View(title: str, nodes: list[ViewNode], edges: list[ViewEdge])`.
  - `filter_graph(graph: Graph, rules: Rules) -> Graph` applies `hide_types`, `hide_managed` and the `collapse` rules. Collapsed edges are undirected, stored with a sorted `(source, target)` pair.
  - `build_detailed(graph: Graph, rules: Rules, title: str = "Detailed topology") -> View`.

- [ ] **Step 1: Write the failing tests**

`tests/test_views.py`:

```python
from infradiag.model import AZURE, BICEP, COLLAPSED, ENDPOINT, REF, STUB, Edge, Graph
from infradiag.rules import CollapseRule, Rules
from infradiag.views import ViewEdge, build_detailed, filter_graph
from tests.factories import DEV, SHARED, SUB, node, rid

VNET = rid(DEV, "Microsoft.Network/virtualNetworks/vnet-app-dev-weu")
SUBNET = VNET + "/subnets/snet-pe"
HUB = rid(SHARED, "Microsoft.Network/virtualNetworks/vnet-app-shared-weu")
PE = rid(DEV, "Microsoft.Network/privateEndpoints/pe-kv")
KV = rid(DEV, "Microsoft.KeyVault/vaults/kv-app-dev-weu")
SQL = rid(DEV, "Microsoft.Sql/servers/sql-app-dev-weu")
DB = SQL + "/databases/app-db"
NIC = rid(DEV, "Microsoft.Network/networkInterfaces/pe-kv.nic.1")
ZONE = rid(SHARED, "Microsoft.Network/privateDnsZones/privatelink.vaultcore.azure.net")
LINK = ZONE + "/virtualNetworkLinks/link-dev"
PEER_A = VNET + "/virtualNetworkPeerings/peer-dev-to-hub"
PEER_B = HUB + "/virtualNetworkPeerings/peer-hub-to-dev"
ROLE = rid(DEV, "Microsoft.Authorization/roleAssignments/ra1")
PLATFORM = rid("rg-app-platform-weu", "Microsoft.Network/dnsZones/example.org")

RULES = Rules(
    hide_types=("microsoft.authorization/*", "microsoft.network/networkinterfaces"),
    hide_managed=True,
    collapse=(
        CollapseRule("microsoft.network/privatednszones/virtualnetworklinks", "virtualNetwork.id", "dns link"),
        CollapseRule("microsoft.network/virtualnetworks/virtualnetworkpeerings", "remoteVirtualNetwork.id", "peering"),
    ),
    icons={"microsoft.keyvault/vaults": "azure/Security Service Color/Key Vaults.svg"},
)


def build_graph():
    g = Graph()
    for n in (
        node(VNET), node(SUBNET), node(HUB), node(KV), node(SQL), node(DB),
        node(PE, sources=[BICEP]),
        node(NIC, sources=[AZURE], managed_by=PE),
        node(ROLE),
        node(ZONE), node(LINK, properties={"virtualNetwork": {"id": VNET}}),
        node(PEER_A, properties={"remoteVirtualNetwork": {"id": HUB}}),
        node(PEER_B, properties={"remoteVirtualNetwork": {"id": VNET}}),
        node(PLATFORM, sources=(), flags={STUB}),
        node(rid(DEV, "Microsoft.App/containerApps/ca-web"), sources=[AZURE]),
    ):
        g.add_node(n)
    g.edges |= {
        Edge(PE.lower(), SUBNET.lower(), REF, "properties.subnet.id"),
        Edge(PE.lower(), KV.lower(), REF, "properties.privateLinkServiceConnections[].properties.privateLinkServiceId"),
        Edge(ROLE.lower(), KV.lower(), REF, "properties.scope"),
        Edge(KV.lower(), PLATFORM.lower(), REF, "properties.x.id"),
        Edge(rid(DEV, "Microsoft.App/containerApps/ca-web").lower(), KV.lower(), ENDPOINT, "endpoint"),
    }
    return g


def test_filter_hides_types_and_managed_and_collapses():
    g = filter_graph(build_graph(), RULES)
    assert ROLE.lower() not in g.nodes
    assert NIC.lower() not in g.nodes
    assert LINK.lower() not in g.nodes and PEER_A.lower() not in g.nodes
    collapsed = {(e.source, e.target, e.label) for e in g.edges if e.kind == COLLAPSED}
    assert collapsed == {
        tuple(sorted((ZONE.lower(), VNET.lower()))) + ("dns link",),
        tuple(sorted((VNET.lower(), HUB.lower()))) + ("peering",),
    }
    assert not any(e.source == ROLE.lower() for e in g.edges)


def find(view_nodes, label):
    for n in view_nodes:
        if n.label == label:
            return n
        if (hit := find(n.children, label)) is not None:
            return hit
    return None


def test_detailed_nests_sub_rg_vnet_subnet_resource():
    view = build_detailed(build_graph(), RULES)
    sub = find(view.nodes, f"Subscription {SUB}")
    rg = find(sub.children, DEV)
    vnet = find(rg.children, "vnet-app-dev-weu")
    subnet = find(vnet.children, "snet-pe")
    assert [c.label for c in subnet.children] == ["pe-kv"]
    assert [c.label for c in find(rg.children, "sql-app-dev-weu").children] == ["app-db"]


def test_detailed_styles_and_icons():
    view = build_detailed(build_graph(), RULES)
    assert find(view.nodes, "pe-kv").style == "bicep_only"
    assert find(view.nodes, "ca-web").style == "azure_only"
    assert find(view.nodes, "kv-app-dev-weu").style == "normal"
    assert find(view.nodes, "kv-app-dev-weu").icon == "azure/Security Service Color/Key Vaults.svg"
    assert find(view.nodes, "kv-app-dev-weu").sublabel == "vaults"


def test_stubs_grouped_under_external():
    view = build_detailed(build_graph(), RULES)
    external = find(view.nodes, "External / out of scope")
    assert [c.label for c in external.children] == ["example.org"]
    assert find(external.children, "example.org").style == "stub"


def test_detailed_edges_drop_placement_duplicates_and_label_collapsed():
    view = build_detailed(build_graph(), RULES)
    keys = {}

    def index(nodes):
        for n in nodes:
            keys[n.key] = n.label
            index(n.children)

    index(view.nodes)
    named = {(keys[e.source], keys[e.target], e.label, e.style) for e in view.edges}
    assert ("pe-kv", "snet-pe", "", "solid") not in named
    assert ("pe-kv", "kv-app-dev-weu", "", "solid") in named
    assert ("ca-web", "kv-app-dev-weu", "", "dashed") in named
    assert any(label == "peering" for *_, label, _ in named)


def test_detailed_is_deterministic():
    assert build_detailed(build_graph(), RULES) == build_detailed(build_graph(), RULES)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_views.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'infradiag.views'`.

- [ ] **Step 3: Implement**

`src/infradiag/views.py`:

```python
"""Turn the merged graph into renderer-agnostic View trees (detailed and summary)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .edges import get_path
from .ids import resolve
from .model import AZURE, BICEP, COLLAPSED, ENDPOINT, STUB, Edge, Graph, Node
from .rules import Rules, matches

SUBNET = "microsoft.network/virtualnetworks/subnets"


@dataclass
class ViewNode:
    key: str
    label: str
    sublabel: str = ""
    icon: str | None = None
    style: str = "normal"
    children: list[ViewNode] = field(default_factory=list)


@dataclass(frozen=True, order=True)
class ViewEdge:
    source: str
    target: str
    label: str = ""
    style: str = "solid"


@dataclass
class View:
    title: str
    nodes: list[ViewNode]
    edges: list[ViewEdge]


def short_type(resource_type: str) -> str:
    return resource_type.rsplit("/", 1)[-1]


def filter_graph(graph: Graph, rules: Rules) -> Graph:
    out = Graph()
    collapsing = {}
    for rid, n in graph.nodes.items():
        if matches(n.type, rules.hide_types) or (rules.hide_managed and n.managed_by):
            continue
        rule = next((r for r in rules.collapse if r.type == n.type), None)
        if rule:
            collapsing[rid] = rule
            continue
        out.nodes[rid] = n
    known = set(out.nodes)
    for rid, rule in sorted(collapsing.items()):
        n = graph.nodes[rid]
        target, parent = get_path(n.properties, rule.target_path), n.parent_id
        if not isinstance(target, str) or parent not in known:
            continue
        resolved = resolve(target, known)
        if resolved in known and resolved != parent:
            a, b = sorted((parent, resolved))
            out.edges.add(Edge(a, b, COLLAPSED, rule.label))
    out.edges |= {e for e in graph.edges if e.source in known and e.target in known}
    return out


def _style(n: Node) -> str:
    if STUB in n.flags:
        return "stub"
    if n.sources == {BICEP}:
        return "bicep_only"
    if n.sources == {AZURE}:
        return "azure_only"
    return "normal"


def _edge_style(e: Edge) -> str:
    return "dashed" if e.kind == ENDPOINT else "solid"


def _break_cycles(placement: dict[str, str | None]) -> None:
    for start in list(placement):
        seen, current = set(), start
        while current is not None:
            if current in seen:
                placement[start] = None
                break
            seen.add(current)
            current = placement.get(current)


def build_detailed(graph: Graph, rules: Rules, title: str = "Detailed topology") -> View:
    g = filter_graph(graph, rules)
    ids = sorted(g.nodes)
    keys = {rid: f"n{i}" for i, rid in enumerate(ids)}
    vnodes = {
        rid: ViewNode(keys[rid], g.nodes[rid].name, short_type(g.nodes[rid].type),
                      rules.icons.get(g.nodes[rid].type), _style(g.nodes[rid]))
        for rid in ids
    }

    subnet_of: dict[str, str] = {}
    for e in sorted(g.edges):
        if (e.kind != COLLAPSED and g.nodes[e.target].type == SUBNET
                and g.nodes[e.source].type != SUBNET and e.source not in subnet_of):
            subnet_of[e.source] = e.target

    placement: dict[str, str | None] = {}
    for rid in ids:
        n = g.nodes[rid]
        if n.type != SUBNET and rid in subnet_of:
            placement[rid] = subnet_of[rid]
        elif n.parent_id in g.nodes:
            placement[rid] = n.parent_id
        else:
            placement[rid] = None
    _break_cycles(placement)

    roots: list[ViewNode] = []
    containers: dict[str, ViewNode] = {}

    def container(cid: str, label: str, sublabel: str, icon: str | None, parent: ViewNode | None) -> ViewNode:
        if cid not in containers:
            containers[cid] = ViewNode(f"c{len(containers)}", label, sublabel, icon)
            (parent.children if parent else roots).append(containers[cid])
        return containers[cid]

    for rid in ids:
        parent = placement[rid]
        if parent is not None:
            vnodes[parent].children.append(vnodes[rid])
            continue
        n = g.nodes[rid]
        if STUB in n.flags:
            holder = container("external", "External / out of scope", "", None, None)
        else:
            sub = container(f"sub:{n.subscription}", f"Subscription {n.subscription}", "subscription",
                            rules.icons.get("_subscription"), None)
            holder = container(f"rg:{n.subscription}/{n.resource_group}", n.resource_group, "resource group",
                               rules.icons.get("_resourcegroup"), sub)
        holder.children.append(vnodes[rid])

    edges = {
        ViewEdge(keys[e.source], keys[e.target], e.label if e.kind == COLLAPSED else "", _edge_style(e))
        for e in g.edges
        if placement.get(e.source) != e.target and e.source != e.target
    }
    return View(title, roots, sorted(edges))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_views.py -v`

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/infradiag/views.py tests/test_views.py
git commit -m "Add graph filtering and detailed view

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Summary view

**Files:**
- Modify: `src/infradiag/views.py` (append `build_summary`)
- Test: `tests/test_views.py` (append)

**Interfaces:**
- Consumes:
  - `filter_graph`, `ViewNode`, `ViewEdge`, `View`, `short_type`, from Task 9
  - `rules.NameCollapse`
- Produces:
  - `build_summary(graph: Graph, rules: Rules, title: str = "Architecture overview") -> View`.
    - Root nodes are group containers (`g0`, `g1`, …) in the `summary_groups` order, then `Other`. Empty groups are omitted.
    - Edge styles: `private` when the target has a private endpoint, `dashed` for endpoint edges, otherwise `solid`.

- [ ] **Step 1: Append failing tests to `tests/test_views.py`**

```python
from infradiag.rules import NameCollapse
from infradiag.views import build_summary

ENV = rid(DEV, "Microsoft.App/managedEnvironments/cae-app-dev-weu")
WRK1 = rid(DEV, "Microsoft.App/containerApps/ca-worker-a-dev-weu")
WRK2 = rid(DEV, "Microsoft.App/containerApps/ca-worker-b-dev-weu")
API = rid(DEV, "Microsoft.App/containerApps/ca-api-dev-weu")
APPCFG = rid(DEV, "Microsoft.AppConfiguration/configurationStores/appcfg-app-dev-weu")
VGW = rid(SHARED, "Microsoft.Network/virtualNetworkGateways/vgw-app-shared-weu")
PIP = rid(SHARED, "Microsoft.Network/publicIPAddresses/pip-app-vgw-shared-weu")
WEIRD = rid(DEV, "Microsoft.Weird/things/thing1")

SUMMARY_RULES = Rules(
    hide_types=RULES.hide_types,
    hide_managed=True,
    summary_groups=(
        ("Connectivity", ("microsoft.network/virtualnetworkgateways",)),
        ("Networking", ("microsoft.network/virtualnetworks", "microsoft.network/publicipaddresses")),
        ("Workloads", ("microsoft.app/*",)),
        ("Data & integration", ("microsoft.keyvault/vaults", "microsoft.appconfiguration/*", "microsoft.sql/servers")),
    ),
    summary_name_overrides=(("pip-app-vgw-*", "Connectivity"),),
    summary_hide_types=("microsoft.network/privateendpoints",),
    summary_nest_into_types=("microsoft.app/managedenvironments",),
    summary_collapse_names=(NameCollapse("ca-worker-*", "Workers"),),
)


def build_summary_graph():
    g = build_graph()
    for n in (node(ENV), node(WRK1), node(WRK2), node(API), node(APPCFG), node(VGW), node(PIP), node(WEIRD)):
        g.add_node(n)
    g.edges |= {
        Edge(WRK1.lower(), ENV.lower(), REF, "properties.environmentId"),
        Edge(WRK2.lower(), ENV.lower(), REF, "properties.environmentId"),
        Edge(API.lower(), ENV.lower(), REF, "properties.environmentId"),
        Edge(API.lower(), APPCFG.lower(), ENDPOINT, "endpoint"),
        Edge(WRK1.lower(), KV.lower(), ENDPOINT, "endpoint"),
        Edge(WRK2.lower(), KV.lower(), ENDPOINT, "endpoint"),
        Edge(WRK1.lower(), WRK1.lower() + "x", REF, "ignored-missing-target"),
    }
    g.edges = {e for e in g.edges if e.target in g.nodes}
    return g


def labels(nodes):
    return [n.label for n in nodes]


def test_summary_groups_in_configured_order_with_other_last():
    view = build_summary(build_summary_graph(), SUMMARY_RULES)
    assert labels(view.nodes) == ["Connectivity", "Networking", "Workloads", "Data & integration", "Other"]
    assert labels(view.nodes[0].children) == ["pip-app-vgw-shared-weu", "vgw-app-shared-weu"]
    assert "thing1" in labels(view.nodes[-1].children)


def test_summary_hides_children_stubs_and_hidden_types():
    view = build_summary(build_summary_graph(), SUMMARY_RULES)
    assert find(view.nodes, "pe-kv") is None
    assert find(view.nodes, "snet-pe") is None
    assert find(view.nodes, "app-db") is None
    assert find(view.nodes, "example.org") is None
    assert find(view.nodes, "sql-app-dev-weu") is not None


def test_summary_collapses_workers_and_nests_apps_in_environment():
    view = build_summary(build_summary_graph(), SUMMARY_RULES)
    env = find(view.nodes, "cae-app-dev-weu")
    assert sorted(labels(env.children)) == ["Workers ×2", "ca-api-dev-weu"]
    assert find(view.nodes, "ca-worker-a-dev-weu") is None


def test_summary_edges_are_merged_and_styled():
    view = build_summary(build_summary_graph(), SUMMARY_RULES)
    keys = {}

    def index(nodes):
        for n in nodes:
            keys[n.key] = n.label
            index(n.children)

    index(view.nodes)
    named = {(keys[e.source], keys[e.target], e.style) for e in view.edges}
    assert ("Workers ×2", "kv-app-dev-weu", "private") in named
    assert ("ca-api-dev-weu", "appcfg-app-dev-weu", "dashed") in named
    assert not any(t == "cae-app-dev-weu" for _, t, _ in named)
    assert len([1 for s, t, _ in named if s == "Workers ×2" and t == "kv-app-dev-weu"]) == 1
    assert all(n.style == "normal" for n in view.nodes)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_views.py -v`

Expected: FAIL — `ImportError: cannot import name 'build_summary'`.

- [ ] **Step 3: Implement — append to `src/infradiag/views.py`**

Add `from fnmatch import fnmatchcase` to the imports, and add `REF` to the model import. Then append:

```python
PRIVATE_ENDPOINT = "microsoft.network/privateendpoints"


def _group_for(n: Node, rules: Rules) -> str:
    for pattern, group in rules.summary_name_overrides:
        if fnmatchcase(n.name.lower(), pattern.lower()):
            return group
    for group, patterns in rules.summary_groups:
        if matches(n.type, patterns):
            return group
    return "Other"


def build_summary(graph: Graph, rules: Rules, title: str = "Architecture overview") -> View:
    g = filter_graph(graph, rules)
    private_targets = {
        e.target for e in g.edges
        if e.kind == REF and g.nodes[e.source].type == PRIVATE_ENDPOINT and "privatelinkserviceid" in e.label.lower()
    }
    keep = {
        rid: n for rid, n in g.nodes.items()
        if STUB not in n.flags and not matches(n.type, rules.summary_hide_types) and n.parent_id not in g.nodes
    }

    rep: dict[str, str] = {}
    members: dict[str, list[str]] = {}
    for rid in sorted(keep):
        r = rid
        for nc in rules.summary_collapse_names:
            if fnmatchcase(keep[rid].name.lower(), nc.pattern.lower()):
                r = f"collapse:{nc.label}"
                break
        rep[rid] = r
        members.setdefault(r, []).append(rid)

    keys = {r: f"n{i}" for i, r in enumerate(sorted(members))}
    vnodes: dict[str, ViewNode] = {}
    for r, ids in members.items():
        first = keep[ids[0]]
        label = f"{r.split(':', 1)[1]} ×{len(ids)}" if r.startswith("collapse:") else first.name
        vnodes[r] = ViewNode(keys[r], label, short_type(first.type), rules.icons.get(first.type))

    nest: dict[str, str] = {}
    for e in sorted(g.edges):
        if e.source in keep and e.target in keep and matches(keep[e.target].type, rules.summary_nest_into_types):
            s, t = rep[e.source], rep[e.target]
            if s != t:
                nest.setdefault(s, t)

    grouped: dict[str, list[ViewNode]] = {}
    for r in sorted(members):
        if r in nest:
            vnodes[nest[r]].children.append(vnodes[r])
        else:
            grouped.setdefault(_group_for(keep[members[r][0]], rules), []).append(vnodes[r])

    order = [name for name, _ in rules.summary_groups] + ["Other"]
    roots = [
        ViewNode(f"g{i}", name, children=grouped[name])
        for i, name in enumerate(n for n in order if n in grouped)
    ]

    styles: dict[tuple[str, str], tuple[str, str]] = {}
    for e in sorted(g.edges):
        if e.source not in keep or e.target not in keep:
            continue
        s, t = rep[e.source], rep[e.target]
        if s == t or nest.get(s) == t:
            continue
        style = "private" if e.target in private_targets else _edge_style(e)
        label = e.label if e.kind == COLLAPSED else ""
        if (s, t) not in styles or style == "private":
            styles[(s, t)] = (label, style)
    edges = sorted(ViewEdge(keys[s], keys[t], label, style) for (s, t), (label, style) in styles.items())
    return View(title, roots, edges)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_views.py -v`

Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add src/infradiag/views.py tests/test_views.py
git commit -m "Add summary view with logical groups

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: D2 renderer

**Files:**
- Create: `src/infradiag/render_d2.py`
- Test: `tests/test_render_d2.py`

**Interfaces:**
- Consumes:
  - `views.View`, `ViewNode`, `ViewEdge`
- Produces:
  - `ICON_BASE = "https://icons.terrastruct.com/"`
  - `icon_url(path: str) -> str` — the whole path is percent-encoded, including `/`.
  - `render_d2(view: View) -> str`
  - `class D2NotFound(Exception)`
  - `render_svg(d2_text: str, d2_file: Path, svg_file: Path) -> None` always writes `d2_file` first. It raises `D2NotFound` if `d2` is missing, or `RuntimeError` with d2's stderr if `d2` fails.

- [ ] **Step 1: Write the failing tests**

`tests/test_render_d2.py`:

```python
import shutil
import subprocess

import pytest

from infradiag import render_d2 as r
from infradiag.views import View, ViewEdge, ViewNode

VIEW = View(
    "T",
    [ViewNode("c0", "rg-a", "resource group", children=[
        ViewNode("n0", "kv-1", "vaults", icon="azure/Security Service Color/Key Vaults.svg", style="azure_only"),
        ViewNode("n1", "privatelink.vaultcore.azure.net", "privatednszones"),
    ])],
    [ViewEdge("n1", "n0", "dns link", "solid"), ViewEdge("n0", "n1", "", "dashed")],
)


def test_render_snapshot():
    text = r.render_d2(VIEW)
    body = text[text.index("title: "):]
    assert body == (
        'title: "T" {near: top-center; shape: text; style.font-size: 28}\n'
        'c0: "rg-a\\nresource group" {\n'
        '  n0: "kv-1\\nvaults" {\n'
        '    icon: "https://icons.terrastruct.com/azure%2FSecurity%20Service%20Color%2FKey%20Vaults.svg"\n'
        '    class: azure_only\n'
        '  }\n'
        '  n1: "privatelink.vaultcore.azure.net\\nprivatednszones"\n'
        '}\n'
        'c0.n1 -> c0.n0: "dns link"\n'
        'c0.n0 -> c0.n1: {class: dashed}\n'
    )


def test_header_defines_every_style_class():
    text = r.render_d2(VIEW)
    for cls in ("bicep_only", "azure_only", "stub", "dashed", "private"):
        assert f"  {cls}: " in text


def test_labels_with_quotes_and_backslashes_are_escaped():
    view = View('a "b"', [ViewNode("n0", 'x"y\\z')], [])
    text = r.render_d2(view)
    assert 'title: "a \\"b\\""' in text
    assert 'n0: "x\\"y\\\\z"' in text


def test_render_svg_without_d2_writes_source_and_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(r.shutil, "which", lambda _: None)
    with pytest.raises(r.D2NotFound, match="brew install d2"):
        r.render_svg("x: y\n", tmp_path / "out" / "v.d2", tmp_path / "out" / "v.svg")
    assert (tmp_path / "out" / "v.d2").read_text() == "x: y\n"


def test_render_svg_surfaces_d2_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(r.shutil, "which", lambda _: "/bin/d2")
    monkeypatch.setattr(r.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "bad syntax"))
    with pytest.raises(RuntimeError, match="bad syntax"):
        r.render_svg("x", tmp_path / "v.d2", tmp_path / "v.svg")


@pytest.mark.skipif(shutil.which("d2") is None, reason="d2 not installed")
def test_real_d2_accepts_output(tmp_path):
    view = View("T", [ViewNode("c0", "rg", children=[ViewNode("n0", "a.b.c"), ViewNode("n1", 'q"x')])],
                [ViewEdge("n0", "n1", "", "private"), ViewEdge("n1", "n0", "peering", "solid")])
    r.render_svg(r.render_d2(view), tmp_path / "v.d2", tmp_path / "v.svg")
    assert "<svg" in (tmp_path / "v.svg").read_text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_render_d2.py -v`

Expected: FAIL — `ImportError: cannot import name 'render_d2'`.

- [ ] **Step 3: Implement**

`src/infradiag/render_d2.py`:

```python
"""Render a View as D2 text, then to SVG with the d2 CLI (ELK layout)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

from .views import View, ViewNode

ICON_BASE = "https://icons.terrastruct.com/"

HEADER = """direction: right
classes: {
  bicep_only: {style.stroke-dash: 4}
  azure_only: {style: {stroke: "#d32f2f"; font-color: "#d32f2f"; stroke-width: 3}}
  stub: {style: {stroke: "#9e9e9e"; font-color: "#9e9e9e"; fill: "#f5f5f5"}}
  dashed: {style.stroke-dash: 3}
  private: {style: {stroke: "#1565c0"; stroke-dash: 3}}
}
"""


class D2NotFound(Exception):
    pass


def _q(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def icon_url(path: str) -> str:
    return ICON_BASE + quote(path, safe="")


def _emit(node: ViewNode, parent_path: str, depth: int, lines: list[str], paths: dict[str, str]) -> None:
    path = f"{parent_path}.{node.key}" if parent_path else node.key
    paths[node.key] = path
    pad = "  " * depth
    label = node.label + (f"\n{node.sublabel}" if node.sublabel else "")
    body = []
    if node.icon:
        body.append(f"{pad}  icon: {_q(icon_url(node.icon))}")
    if node.style != "normal":
        body.append(f"{pad}  class: {node.style}")
    if not body and not node.children:
        lines.append(f"{pad}{node.key}: {_q(label)}")
        return
    lines.append(f"{pad}{node.key}: {_q(label)} {{")
    lines.extend(body)
    for child in node.children:
        _emit(child, path, depth + 1, lines, paths)
    lines.append(f"{pad}}}")


def render_d2(view: View) -> str:
    lines = [HEADER.rstrip("\n"), f"title: {_q(view.title)} {{near: top-center; shape: text; style.font-size: 28}}"]
    paths: dict[str, str] = {}
    for node in view.nodes:
        _emit(node, "", 0, lines, paths)
    for edge in view.edges:
        line = f"{paths[edge.source]} -> {paths[edge.target]}"
        if edge.label and edge.style != "solid":
            line += f": {_q(edge.label)} {{class: {edge.style}}}"
        elif edge.label:
            line += f": {_q(edge.label)}"
        elif edge.style != "solid":
            line += f": {{class: {edge.style}}}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def render_svg(d2_text: str, d2_file: Path, svg_file: Path) -> None:
    d2_file.parent.mkdir(parents=True, exist_ok=True)
    d2_file.write_text(d2_text)
    exe = shutil.which("d2")
    if exe is None:
        raise D2NotFound(f"d2 not installed — `brew install d2` (see https://d2lang.com). Source written to {d2_file}")
    proc = subprocess.run([exe, "--layout", "elk", str(d2_file), str(svg_file)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"d2 failed for {d2_file}:\n{proc.stderr.strip()}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_render_d2.py -v`

Expected: 5 passed, 1 skipped (if `d2` isn't installed yet).

- [ ] **Step 5: Commit**

```bash
git add src/infradiag/render_d2.py tests/test_render_d2.py
git commit -m "Add D2 renderer

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: CLI wiring and README

**Files:**
- Create: `src/infradiag/cli.py`
- Create: `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `cli.build_graphs(cfg: Config) -> tuple[Graph, Graph, WhatIfSignals]`. Both graphs have reference and endpoint edges added.
  - `cli.main(argv: list[str] | None = None) -> int` returns:
    - 0 on success;
    - 1 if extract had failures;
    - 2 on a user-facing error (`ConfigError`, `CacheMissing`, `AzError`, `D2NotFound`, `RuntimeError`), printed as `error: ...` to stderr.
  - Subcommands:
    - `extract [--bicep | --azure]`
    - `diff` → `out/drift-report.md`
    - `render [--view detailed|summary|all]` → `out/<view>.d2` and `out/<view>.svg`
  - Global options: `--config` (default `config.yaml`) and `--views` (default `views.yaml`).

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py`:

```python
import json
from pathlib import Path

from infradiag import cli, render_d2
from tests.factories import DEV, arg_row, rid, whatif_change, whatif_result

KV = rid(DEV, "Microsoft.KeyVault/vaults/kv-app-dev-weu")
WEB = rid(DEV, "Microsoft.App/containerApps/ca-web-dev-weu")

CONFIG = f"""
bicep_root: /nonexistent
subscription: 0a1b2c3d-0000-4000-8000-000000000001
location: westeurope
resource_groups: [{DEV}]
entry_points:
  - name: main-dev
    template: infra/main.bicep
    resource_groups: [{DEV}]
"""

VIEWS = """
hide_types: []
summary:
  groups:
    Data: [microsoft.keyvault/vaults]
"""


def setup(tmp_path: Path) -> list[str]:
    (tmp_path / "config.yaml").write_text(CONFIG)
    (tmp_path / "views.yaml").write_text(VIEWS)
    (tmp_path / "cache" / "whatif").mkdir(parents=True)
    (tmp_path / "cache" / "whatif" / "main-dev.json").write_text(
        json.dumps(whatif_result(whatif_change("NoChange", KV), whatif_change("Ignore", WEB))))
    (tmp_path / "cache" / "arg.json").write_text(json.dumps(
        {"resource_groups": [DEV], "rows": [arg_row(KV), arg_row(WEB)]}))
    return ["--config", str(tmp_path / "config.yaml"), "--views", str(tmp_path / "views.yaml")]


def test_diff_writes_report(tmp_path, capsys):
    assert cli.main(setup(tmp_path) + ["diff"]) == 0
    report = (tmp_path / "out" / "drift-report.md").read_text()
    assert "| Matched | 1 |" in report
    assert "| ca-web-dev-weu |" in report
    assert "confirmed by what-if Ignore" in report
    assert "drift-report.md" in capsys.readouterr().out


def test_render_without_d2_writes_sources_and_reports_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(render_d2.shutil, "which", lambda _: None)
    assert cli.main(setup(tmp_path) + ["render"]) == 2
    assert (tmp_path / "out" / "detailed.d2").exists()
    assert "error: d2 not installed" in capsys.readouterr().err


def test_render_single_view(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "render_svg", lambda text, d2, svg: calls.append(svg.name))
    assert cli.main(setup(tmp_path) + ["render", "--view", "summary"]) == 0
    assert calls == ["summary.svg"]


def test_missing_cache_is_a_friendly_error(tmp_path, capsys):
    args = setup(tmp_path)
    (tmp_path / "cache" / "arg.json").unlink()
    assert cli.main(args + ["diff"]) == 2
    assert "infradiag extract --azure" in capsys.readouterr().err


def test_extract_reports_partial_failure(tmp_path, monkeypatch):
    args = setup(tmp_path)
    monkeypatch.setattr(cli, "extract_bicep", lambda cfg, **kw: {"main-dev": "boom"})
    monkeypatch.setattr(cli, "extract_azure", lambda cfg, **kw: 2)
    assert cli.main(args + ["extract"]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v`

Expected: FAIL — `ImportError: cannot import name 'cli'`.

- [ ] **Step 3: Implement**

`src/infradiag/cli.py`:

```python
"""infradiag: diagrams and a drift report from Bicep what-if and Azure Resource Graph."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .azcli import AzError
from .cache import CacheMissing
from .config import Config, ConfigError, load_config
from .diff import compute_drift, render_markdown
from .edges import add_endpoint_edges, add_reference_edges
from .extract_azure import extract_azure, load_arg_cache
from .extract_bicep import extract_bicep, load_whatif_cache
from .model import Graph, merge_graphs
from .normalise import WhatIfSignals, graph_from_arg, graph_from_whatif
from .render_d2 import D2NotFound, render_d2, render_svg
from .rules import load_rules
from .views import build_detailed, build_summary

VIEWS = {"detailed": build_detailed, "summary": build_summary}


def build_graphs(cfg: Config) -> tuple[Graph, Graph, WhatIfSignals]:
    bicep, signals = graph_from_whatif(load_whatif_cache(cfg))
    azure = graph_from_arg(load_arg_cache(cfg))
    for graph in (bicep, azure):
        add_reference_edges(graph)
        add_endpoint_edges(graph)
    return bicep, azure, signals


def _extract(cfg: Config, args) -> int:
    both = not args.bicep and not args.azure
    failures = {}
    if args.bicep or both:
        failures = extract_bicep(cfg)
    if args.azure or both:
        print(f"Resource Graph: {extract_azure(cfg)} rows")
    if failures:
        print(f"{len(failures)} what-if failure(s): {', '.join(sorted(failures))}", file=sys.stderr)
        return 1
    return 0


def _diff(cfg: Config, rules) -> int:
    bicep, azure, signals = build_graphs(cfg)
    report = compute_drift(bicep, azure, signals, rules, cfg)
    names = {rid: n.name for rid, n in merge_graphs(bicep, azure).nodes.items()}
    out = cfg.out_dir / "drift-report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(report, names))
    print(f"{out}: {report.matched} matched, {len(report.azure_only)} Azure-only, "
          f"{len(report.bicep_only)} Bicep-only, {len(report.modified)} modified")
    return 0


def _render(cfg: Config, rules, which: str) -> int:
    bicep, azure, _ = build_graphs(cfg)
    merged = merge_graphs(bicep, azure)
    for name in (VIEWS if which == "all" else [which]):
        view = VIEWS[name](merged, rules)
        render_svg(render_d2(view), cfg.out_dir / f"{name}.d2", cfg.out_dir / f"{name}.svg")
        print(cfg.out_dir / f"{name}.svg")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="infradiag", description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--views", type=Path, default=Path("views.yaml"))
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("extract", help="run what-if and Resource Graph (needs az login)")
    group = ex.add_mutually_exclusive_group()
    group.add_argument("--bicep", action="store_true")
    group.add_argument("--azure", action="store_true")
    sub.add_parser("diff", help="write out/drift-report.md from the cache")
    rd = sub.add_parser("render", help="write out/<view>.d2 and .svg from the cache")
    rd.add_argument("--view", choices=["detailed", "summary", "all"], default="all")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
        if args.command == "extract":
            return _extract(cfg, args)
        rules = load_rules(args.views)
        if args.command == "diff":
            return _diff(cfg, rules)
        return _render(cfg, rules, args.view)
    except (ConfigError, CacheMissing, AzError, D2NotFound, RuntimeError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
```

`README.md`:

````markdown
# infradiag

Diagrams and a drift report for an Azure platform, built from Bicep (`az deployment sub what-if`) and checked against Azure (Resource Graph). Reads `~/Projects/bicep-project/infra`; never writes there.

## Setup

```bash
uv sync
brew install d2      # renderer
az login             # Contoso-Dev; Contributor is enough for what-if
```

## Use

```bash
uv run infradiag extract          # ~3-6 min: what-if per entry point + Resource Graph → cache/
uv run infradiag diff             # → out/drift-report.md
uv run infradiag render           # → out/detailed.svg, out/summary.svg (+ .d2 sources)
```

`diff` and `render` run offline from `cache/`, so iterate on `views.yaml` without re-extracting.

- `config.yaml` — entry points (mirrors `bicep-project/pipelines`) and resource groups.
- `views.yaml` — what to hide, collapse, group and which icons to use; drift-report exclusions.

Design: `docs/superpowers/specs/2026-10-09-infra-diagrams-design.md`.
````

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -v`

Expected: all tests pass (one skipped if `d2` isn't installed).

- [ ] **Step 5: Commit**

```bash
git add src/infradiag/cli.py README.md tests/test_cli.py
git commit -m "Wire up infradiag CLI

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Live end-to-end run and tuning (PoC acceptance)

**Files:**
- Modify: `views.yaml`, `src/infradiag/edges.py` (`RUNTIME_REF_PATHS` only), as tuning requires.

**Interfaces:**
- Consumes: the CLI from Task 12.
- Produces: `out/detailed.svg`, `out/summary.svg` and `out/drift-report.md` for user review.

- [ ] **Step 1: Install d2 and confirm the real-d2 test now runs**

```bash
brew install d2 && d2 --version
uv run pytest tests/test_render_d2.py -v
```

Expected: 6 passed (no skips).

- [ ] **Step 2: Verify every icon URL in `views.yaml` resolves**

```bash
uv run python - <<'EOF'
import urllib.request, yaml
from infradiag.render_d2 import icon_url
for t, p in yaml.safe_load(open("views.yaml"))["icons"].items():
    try:
        code = urllib.request.urlopen(urllib.request.Request(icon_url(p), method="HEAD")).status
    except Exception as e:
        code = e
    print(code, t)
EOF
```

Expected: `200` for every line. Fix or remove any path that isn't 200.

- [ ] **Step 3: Extract (live, read-only — what-if previews only)**

Run: `uv run infradiag extract`

Expected:

- Six `what-if …` lines, then `Resource Graph: N rows` with N around 180.
- Exit code 0.

If any entry point fails, read its `cache/whatif/<name>.json`, fix `config.yaml`, and re-run `uv run infradiag extract --bicep`.

- [ ] **Step 4: Diff and review the drift report**

Run: `uv run infradiag diff`, then read `out/drift-report.md`.

Expected:

- `ca-web-dev-weu`, `ca-reports-dev-weu` and `ca-notify-dev-weu` appear in *In Azure, not in Bicep*, each confirmed by Ignore.
- NICs and the App Insights dashboard appear under *Expected implicit*.

Tuning loop:

- **Expected-implicit noise elsewhere** (e.g. the Container Apps environment's auto-created private DNS zone `*.azurecontainerapps.io`) → add its type or name pattern to `implicit_types`.
- **Configuration drift that is only template/runtime noise** → add the path to `ignore_modify_paths`.
- **Azure-only edges that are runtime back-references** → add the label pattern to `RUNTIME_REF_PATHS` in `edges.py`.

Re-run `uv run infradiag diff` after each change; there is no need to re-extract.

- [ ] **Step 5: Render and review the diagrams**

Run: `uv run infradiag render`, then open both SVGs:

```bash
explorer.exe "$(wslpath -w out/detailed.svg)"; explorer.exe "$(wslpath -w out/summary.svg)"
```

Check:

- **Detailed:** RG → VNet → subnet nesting is correct, and PEs sit in `snet-pe`.
- **Detailed:** Azure-only apps show in red.
- **Summary:** the groups read sensibly.

If `detailed.svg` is unreadable as one diagram, the spec says to split it into `detailed-hub.svg` and `detailed-dev.svg`. Do not improvise this here. Stop, show the user the SVG, and if they agree, add it as a follow-up task with tests. That task would call `build_detailed` on the merged graph restricted to one RG's nodes, plus stubs for cross-RG edges.

- [ ] **Step 6: Run the full suite and commit the tuning**

```bash
uv run pytest -v
git add views.yaml src/infradiag/edges.py
git commit -m "Tune view and drift rules against live dev + hub

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 7: Hand over for review**

Give the user the Windows paths to `out/drift-report.md`, `out/detailed.svg` and `out/summary.svg` (`wslpath -w`). Ask them to sanity-check the drift report against their knowledge of the estate. That review is the PoC acceptance criterion.
