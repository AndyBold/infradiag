import json

import pytest

from infradiag import cli, nav
from infradiag.presets import FULL, PRESETS
from tests.factories import SUB, fake_svg
from tests.test_cli import capture_d2, setup


def run(args, extra=()):
    return cli.main(args + ["build", *extra])


def test_full_preset_covers_the_requested_renders():
    flags = [step.command() for step in PRESETS["full"]]
    assert "--view detailed --security-overlay all" in flags
    assert "--view summary --security-overlay all" in flags
    assert "--view application --style c4 --security-overlay identity --split environment" in flags
    assert "--view context --style c4 --security-overlay identity --split environment" in flags
    assert "--view application --style c4 --security-overlay identity --split environment " \
           "--hide-scope subscription --hide-scope management-group --role-plane data" in flags
    assert "--view application --style c4 --security-overlay identity --split environment --drift-only" in flags
    assert PRESETS["full"] is FULL


def test_dry_run_lists_the_steps_and_renders_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "render_svg", lambda *a, **k: pytest.fail("rendered"))
    assert run(setup(tmp_path), ["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"1. {FULL[0].name}: render --view detailed" in out and out.count("\n") == len(FULL)
    assert not (tmp_path / "out").exists()


def test_one_subscription_gets_its_own_application_folder_and_one_nav_group(tmp_path, monkeypatch, capsys):
    capture_d2(monkeypatch)
    assert run(setup(tmp_path, test_sub=True), ["--subscription", SUB]) == 0
    out = tmp_path / "out"
    mine = out / "Contoso-Dev"
    assert (mine / "detailed-all.svg").exists() and (mine / "summary-all.svg").exists()
    assert (mine / "applications/application-c4-identity-no-sub-no-mg-data-index.html").exists()
    assert (mine / "applications/application-c4-identity-drift-index.html").exists()
    assert (mine / "applications/application-c4-identity-index.html").exists()
    assert (mine / "applications/context-c4-identity-index.html").exists()
    assert not (out / "applications").exists()
    groups = {g["name"]: g for g in json.loads((out / "nav.js").read_text().split("= ", 1)[1].rsplit(";", 1)[0])["groups"]}
    assert set(groups) == {"Contoso-Dev", "Contoso-Dev/applications"}
    assert groups["Contoso-Dev/applications"]["depth"] == 1 and groups["Contoso-Dev/applications"]["label"] == "applications"
    assert groups["Contoso-Dev"].get("depth") is None
    text = capsys.readouterr()
    assert f"step 1/{len(FULL)}" in text.err and f"step {len(FULL)}/{len(FULL)}" in text.err
    assert f"build full: {len(FULL)}/{len(FULL)} steps succeeded" in text.out
    assert "FAILED" not in text.out


def test_several_subscriptions_keep_the_combined_application_folder(tmp_path, monkeypatch):
    capture_d2(monkeypatch)
    assert run(setup(tmp_path, test_sub=True)) == 0
    out = tmp_path / "out"
    assert (out / "applications/application-c4-identity-index.html").exists()
    assert (out / "Contoso-Dev/detailed-all.svg").exists() and (out / "Contoso-Test/detailed-all.svg").exists()
    assert not (out / "Contoso-Dev/applications").exists()


def test_a_failed_step_does_not_stop_the_rest_and_the_exit_is_non_zero(tmp_path, monkeypatch, capsys):
    def flaky(text, d2, svg, **kw):
        if svg.name.startswith("summary"):
            raise RuntimeError("d2 gave up")
        d2.parent.mkdir(parents=True, exist_ok=True)
        d2.write_text(text)
        fake_svg(svg)
    monkeypatch.setattr(cli, "render_svg", flaky)
    assert run(setup(tmp_path), ["--subscription", SUB, "--d2-timeout", "5"]) == 2
    text = capsys.readouterr()
    assert f"build full: {len(FULL) - 1}/{len(FULL)} steps succeeded" in text.out
    assert text.out.count("FAILED") == 1 and f"2. FAILED {FULL[1].name}" in text.out
    assert "d2 gave up" in text.err
    mine = tmp_path / "out/Contoso-Dev"
    assert (mine / "detailed-all.svg").exists() and (mine / "applications/context-c4-identity-index.html").exists()
    assert (mine / "applications/application-c4-identity-drift-index.html").exists()
    assert (tmp_path / "out/index.html").exists()  # master page refreshed after a failure


def test_every_step_failing_still_summarises_and_refreshes_the_master_page(tmp_path, monkeypatch, capsys):
    def broken(*a, **k):
        raise RuntimeError("no d2")
    monkeypatch.setattr(cli, "render_svg", broken)
    assert run(setup(tmp_path), ["--subscription", SUB]) == 2
    out = capsys.readouterr().out
    assert f"build full: 0/{len(FULL)} steps succeeded" in out and out.count("FAILED") == len(FULL)
    assert (tmp_path / "out/index.html").exists()


def test_d2_timeout_is_passed_to_every_step(tmp_path, monkeypatch):
    seen = []
    def fake(text, d2, svg, timeout=None, **kw):
        seen.append(timeout)
        d2.parent.mkdir(parents=True, exist_ok=True)
        fake_svg(svg)
    monkeypatch.setattr(cli, "render_svg", fake)
    assert run(setup(tmp_path), ["--subscription", SUB, "--d2-timeout", "7"]) == 0
    assert seen and set(seen) == {7}


def test_build_rejects_a_bad_timeout(tmp_path, capsys):
    with pytest.raises(SystemExit):
        run(setup(tmp_path), ["--d2-timeout", "0"])


def test_missing_cache_is_a_friendly_error(tmp_path, capsys):
    assert run(["--config", str(tmp_path / "x.yaml")]) == 2


def test_nav_keeps_a_folders_subfolders_directly_after_it():
    pages = [nav.Page(p, p, "diagram") for p in ("a-b/x.html", "a/x.html", "a/applications/y.html", "z/applications/y.html")]
    names = [(g["name"], g.get("depth"), g.get("label")) for g in nav.build_tree(pages)]
    assert names == [("a", None, None), ("a/applications", 1, "applications"), ("a-b", None, None),
                     ("z/applications", None, None)]


def corrupt(tmp_path, name):
    (tmp_path / "cache" / SUB / name).write_text('{"rows": [')


def test_a_corrupt_cache_file_fails_its_steps_cleanly_and_names_the_file(tmp_path, monkeypatch, capsys):
    capture_d2(monkeypatch)
    args = setup(tmp_path)
    corrupt(tmp_path, "arg.json")
    assert run(args, ["--subscription", SUB]) == 2
    text = capsys.readouterr()
    assert f"build full: 0/{len(FULL)} steps succeeded" in text.out
    assert "Traceback" not in text.err and "arg.json is not valid JSON" in text.err and "infradiag extract" in text.err


def test_a_corrupt_whatif_cache_fails_only_the_steps_that_need_it(tmp_path, monkeypatch, capsys):
    capture_d2(monkeypatch)
    args = setup(tmp_path)
    (tmp_path / "cache" / SUB / "whatif" / "main-dev.json").write_text("not json")
    assert run(args, ["--subscription", SUB]) == 2
    assert "main-dev.json is not valid JSON" in capsys.readouterr().err


def test_plain_render_reports_a_corrupt_cache_file_cleanly(tmp_path, monkeypatch, capsys):
    capture_d2(monkeypatch)
    args = setup(tmp_path)
    corrupt(tmp_path, "arg.json")
    assert cli.main(args + ["render", "--subscription", SUB]) == 2
    assert "arg.json is not valid JSON" in capsys.readouterr().err
    assert cli.main(args + ["render", "--view", "application", "--subscription", SUB]) == 2
    assert "Traceback" not in capsys.readouterr().err
