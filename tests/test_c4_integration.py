"""Synthetic Azure fixtures validate the complete extraction-to-render projection."""
import json
import shutil
from pathlib import Path

import pytest

from infradiag.applications import build_application
from infradiag.config import DEFAULT_VIEWS
from infradiag.edges import add_reference_edges
from infradiag.normalise import graph_from_arg
from infradiag.overlays import apply_overlay
from infradiag.render_d2 import render_d2, render_svg
from infradiag.rules import load_rules
from infradiag.security import Grant, SecurityEvidence
from infradiag.views import build_detailed, build_summary, project

FIXTURES = Path(__file__).parent / 'fixtures/c4_security'


def fixture_view(level, overlay):
    graph = graph_from_arg(json.loads((FIXTURES / 'resources.json').read_text()))
    add_reference_edges(graph)
    rules = load_rules(DEFAULT_VIEWS)
    if level in ('application', 'context'):
        projection = build_application(graph, rules, level=level, subscription_names={})
    else:
        view = build_detailed(graph, rules) if level == 'detailed' else build_summary(graph, rules)
        projection = project(view, graph)
    evidence = SecurityEvidence(grants=[Grant(**g) for g in json.loads((FIXTURES / 'grants.json').read_text())],
                                coverage=['Synthetic fixture; not an Azure access audit.'])
    return apply_overlay(projection, graph, evidence, mode=overlay, rules=rules)


def test_inline_subnet_security_configuration_is_preserved():
    text = render_d2(fixture_view('application', 'network'), style='c4')
    assert 'apps → orders-nsg: networkSecurityGroup association' in text


@pytest.mark.skipif(shutil.which('d2') is None, reason='d2 not installed')
@pytest.mark.parametrize('level', ['detailed', 'summary', 'application', 'context'])
@pytest.mark.parametrize('overlay', ['none', 'identity', 'network', 'all'])
def test_real_d2_c4_security_matrix(tmp_path, level, overlay):
    stem = f'{level}-{overlay}'
    text = render_d2(fixture_view(level, overlay), style='c4')
    render_svg(text, tmp_path / f'{stem}.d2', tmp_path / f'{stem}.svg')
    svg = (tmp_path / f'{stem}.svg').read_text()
    assert '<svg' in svg
    assert 'Orders' in svg or 'orders' in svg
    if overlay in ('identity', 'all'):
        assert 'Key Vault Secrets User' in svg
