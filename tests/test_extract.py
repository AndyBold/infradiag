import json
import subprocess
from pathlib import Path

import pytest

from infradiag import azcli
from infradiag.azcli import AzError, run_az
from infradiag.cache import CacheMissing
from infradiag.config import ConfigError, EntryPoint, Target
from infradiag.extract_azure import build_query, extract_azure, load_arg_cache
from infradiag.extract_bicep import extract_bicep, load_whatif_cache, whatif_args

SUB = "0a1b2c3d-0000-4000-8000-000000000001"


def make_cfg(tmp_path: Path, resource_groups=("rg-app-dev-weu", "rg-app-shared-weu")) -> Target:
    eps = (
        EntryPoint("network-dev", "infra-network-dev", "infra/network.bicep",
                   ("environmentName=dev",), ("rg-app-dev-weu",)),
        EntryPoint("main-dev", "infra-dev", "infra/main.bicep",
                   ("infra/env/dev.bicepparam", "apiImageTag=1.2.3"), ("rg-app-dev-weu",)),
    )
    return Target(tmp_path / "bicep-project", SUB, "Contoso-Dev", "westeurope",
                  resource_groups, eps, tmp_path / "cache" / SUB, tmp_path / "out" / "Contoso-Dev")


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
    cfg.bicep_root.mkdir()
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


def test_extract_bicep_needs_bicep_root(tmp_path):
    with pytest.raises(ConfigError, match="bicep_root .*bicep-project is not a directory"):
        extract_bicep(make_cfg(tmp_path), run=lambda args, cwd=None: 1 / 0, log=lambda *_: None)


def test_load_whatif_cache_names_missing_entry_points(tmp_path):
    with pytest.raises(CacheMissing, match=f"network-dev, main-dev .*infradiag extract --bicep --subscription {SUB}"):
        load_whatif_cache(make_cfg(tmp_path))


def test_build_query_scopes_subscription_and_rgs(tmp_path):
    q = build_query(make_cfg(tmp_path))
    assert f"subscriptionId =~ '{SUB}'" in q
    assert "resourceGroup in~ ('rg-app-dev-weu', 'rg-app-shared-weu')" in q
    assert "managedBy" in q and "order by id asc" in q


def test_build_query_without_rgs_reads_whole_subscription(tmp_path):
    q = build_query(make_cfg(tmp_path, resource_groups=()))
    assert f"subscriptionId =~ '{SUB}'" in q
    assert "resourceGroup" not in q.split("| project")[0]


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


def test_extract_azure_caches_whole_subscription_scope(tmp_path):
    cfg = make_cfg(tmp_path, resource_groups=())
    extract_azure(cfg, run=lambda args, cwd=None: {"data": [{"id": "a"}]})
    assert json.loads((cfg.cache_dir / "arg.json").read_text())["resource_groups"] is None
    assert load_arg_cache(cfg) == [{"id": "a"}]
    with pytest.raises(CacheMissing, match="read for every resource group, the config now asks for rg-app-dev-weu"):
        load_arg_cache(make_cfg(tmp_path, resource_groups=("rg-app-dev-weu",)))
