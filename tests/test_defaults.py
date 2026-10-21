"""What infradiag ships must suit any Azure/Bicep project: no project's names, IDs or paths."""

import re
from pathlib import Path

from infradiag import cli
from infradiag.config import CONFIG_NAME, DEFAULT_VIEWS, DEFAULTS_DIR, load_config
from infradiag.rules import load_rules

PACKAGE = Path(cli.__file__).parent
SUBSCRIPTION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def shipped_text() -> dict[Path, str]:
    return {p: p.read_text() for p in PACKAGE.rglob("*") if p.suffix in (".py", ".yaml")}


def test_shipped_code_and_defaults_hold_no_subscription_ids_or_home_paths():
    for path, text in shipped_text().items():
        assert not SUBSCRIPTION_ID.search(text), path
        assert "~/" not in text and "/home/" not in text, path


def test_starter_config_configures_no_subscriptions_and_keeps_paths_local(tmp_path):
    (tmp_path / CONFIG_NAME).write_text((DEFAULTS_DIR / CONFIG_NAME).read_text())
    cfg = load_config(tmp_path / CONFIG_NAME)
    assert cfg.subscriptions == {}
    assert cfg.bicep_root == tmp_path
    assert cfg.views_path == tmp_path / "views.yaml"
    assert (cfg.cache_dir, cfg.out_dir) == (tmp_path / "cache", tmp_path / "out")


def test_default_views_have_no_name_rules():
    rules = load_rules(DEFAULT_VIEWS)
    assert (rules.out_of_scope_names, rules.summary_name_overrides, rules.summary_collapse_names) == ((), (), ())


def test_init_writes_starter_files_that_load(tmp_path, capsys):
    project = tmp_path / "project"
    assert cli.main(["init", str(project)]) == 0
    assert (project / "views.yaml").read_text() == DEFAULT_VIEWS.read_text()
    cfg = load_config(project / CONFIG_NAME)
    assert cfg.views_path == project / "views.yaml"
    assert f"--config {project / CONFIG_NAME}" in capsys.readouterr().out


def test_init_refuses_to_overwrite_without_force(tmp_path, capsys):
    (tmp_path / CONFIG_NAME).write_text("bicep_root: mine\n")
    assert cli.main(["init", str(tmp_path)]) == 2
    assert "already exist" in capsys.readouterr().err
    assert (tmp_path / CONFIG_NAME).read_text() == "bicep_root: mine\n"
    assert cli.main(["init", str(tmp_path), "--force"]) == 0
    assert (tmp_path / CONFIG_NAME).read_text() == (DEFAULTS_DIR / CONFIG_NAME).read_text()


def test_commands_find_the_config_in_the_current_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["diff"]) == 2
    assert "no infradiag.yaml in" in capsys.readouterr().err
    (tmp_path / CONFIG_NAME).write_text("")
    assert cli.main(["diff"]) == 2
    assert "infradiag extract" in capsys.readouterr().err  # found the config; nothing extracted yet
