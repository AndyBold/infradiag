import io

from infradiag import cli
from infradiag.progress import Progress
from tests.test_cli import setup


class FakeStream(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


def test_non_tty_emits_plain_lines_without_carriage_returns():
    out = FakeStream(tty=False)
    p = Progress(out)
    p.line("phase one")
    p.step("item", 1, 2)
    p.step("item", 2, 2)
    p.clear()
    assert out.getvalue() == "phase one\nitem 1/2\nitem 2/2\n"


def test_tty_updates_one_line_in_place_and_clears_before_permanent_lines():
    out = FakeStream(tty=True)
    p = Progress(out)
    p.step("item", 1, 2)
    p.step("item", 2, 2)
    p.line("next phase")
    text = out.getvalue()
    assert text.count("\n") == 1 and text.endswith("next phase\n")
    assert "\r\033[Kitem 1/2" in text and "\r\033[Kitem 2/2" in text
    assert text.index("item 2/2") < text.index("next phase")


def test_clear_is_a_noop_with_nothing_on_screen():
    out = FakeStream(tty=True)
    Progress(out).clear()
    assert out.getvalue() == ""


def test_render_reports_phases_on_stderr_and_keeps_stdout_clean(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "render_svg", lambda text, d2, svg, **kw: None)
    assert cli.main(setup(tmp_path) + ["render", "--subscription", "Contoso-Dev"]) == 0
    captured = capsys.readouterr()
    assert "loading cache and building graphs" in captured.err
    assert "view 1/2: detailed" in captured.err and "view 2/2: summary" in captured.err
    assert "d2: laying out detailed.svg" in captured.err and "d2: summary.svg done in" in captured.err
    assert "\r" not in captured.err
    assert "view 1/2" not in captured.out and captured.out.count(".svg") == 2


def test_render_application_reports_per_subscription_progress(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "render_svg", lambda text, d2, svg, **kw: None)
    args = setup(tmp_path, test_sub=True)
    assert cli.main(args + ["render", "--view", "application", "--style", "c4",
                            "--security-overlay", "none"]) in (0, 2)
    err = capsys.readouterr().err
    assert "loading caches, subscription 1/2" in err and "loading caches, subscription 2/2" in err
    assert "building application view" in err and "d2: laying out application-c4.svg" in err


def test_render_reports_view_size_before_layout_and_passes_timeout(tmp_path, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(cli, "render_svg", lambda text, d2, svg, **kw: seen.append(kw))
    assert cli.main(setup(tmp_path) + ["render", "--view", "summary", "--d2-timeout", "90"]) == 0
    err = capsys.readouterr().err
    assert "d2: laying out summary.svg: " in err and " nodes/containers, " in err and " edges" in err
    assert "warning" not in err
    assert seen[0]["timeout"] == 90 and callable(seen[0]["on_tick"])


def test_large_edge_count_warns_on_stderr_before_layout(tmp_path, monkeypatch, capsys):
    from infradiag.views import View, ViewEdge, ViewNode
    nodes = [ViewNode(f"n{i}", f"n{i}") for i in range(3)]
    edges = [ViewEdge("n0", "n1", f"e{i}") for i in range(cli.EDGE_WARN + 1)]
    monkeypatch.setattr(cli, "render_svg", lambda *a, **kw: None)
    cli._d2_svg(Progress(io.StringIO()), "x", tmp_path / "v.d2", tmp_path / "v.svg", View("T", nodes, edges))
    captured = capsys.readouterr()
    assert f"{cli.EDGE_WARN + 1} edges" in captured.err and "--d2-timeout" in captured.err and captured.out == ""


def test_tick_prints_elapsed_line_via_progress(tmp_path, monkeypatch):
    from infradiag.views import View
    out = FakeStream(tty=False)
    monkeypatch.setattr(cli, "render_svg", lambda text, d2, svg, on_tick, **kw: on_tick(125.0))
    cli._d2_svg(Progress(out), "x", tmp_path / "v.d2", tmp_path / "v.svg", View("T", [], []))
    assert "still laying out v.svg (2 min elapsed)" in out.getvalue()
