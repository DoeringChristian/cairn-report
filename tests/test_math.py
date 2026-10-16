"""Report math: LaTeX becomes MathML at build time, and LaTeX the converter cannot render fails the build."""

import pytest

from cairn.config import load_config
from cairn.reports.build import to_mathml
from conftest import write_config

SIZES = [r"\big", r"\Big", r"\bigg", r"\Bigg"]
DELIMITERS = {
    (r"\lVert", r"\rVert"): "‖",
    (r"\lvert", r"\rvert"): "|",
    (r"\langle", r"\rangle"): "⟨",
    (r"\{", r"\}"): "{",
    (r"\lfloor", r"\rfloor"): "⌊",
    (r"\lceil", r"\rceil"): "⌈",
}


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("pair", list(DELIMITERS))
def test_sized_delimiter_commands_become_their_symbols(size, pair):
    left, right = pair
    mathml = to_mathml(f"{size}{left} x {size}{right}_1", "inline")
    assert "\\" not in mathml and DELIMITERS[pair] in mathml.replace("&#x02016;", "‖")


def test_the_norm_of_the_parallax_report_renders():
    formula = (
        r"C(q, z) = \frac{1}{\lvert E\rvert} \sum_{(j,k)\in E} \big\lVert \log(1 + y_j(p_j(z)))"
        r" - \log(1 + y_k(p_k(z))) \big\rVert_1 ,"
    )
    mathml = to_mathml(formula, "block")
    assert "\\" not in mathml and mathml.count("‖") == 2


@pytest.mark.parametrize("latex", [r"\notacommand x", r"\big\notadelimiter x"])
def test_latex_left_as_text_is_an_error(latex):
    with pytest.raises(ValueError, match="unsupported LaTeX"):
        to_mathml(latex, "inline")


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.delenv("CAIRN_DIR", raising=False)
    folder = tmp_path / "proj"
    folder.mkdir()
    write_config(folder, "")
    monkeypatch.chdir(folder)
    return folder.resolve()


def build(proj, body: str):
    from cairn.reports import build as builder

    folder = proj / "reports" / "m"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "report.md").write_text(f"---\ntitle: M\n---\n\n## Math\n\n{body}\n")
    builder.configure(load_config())
    builder.build_all()
    return (proj / ".cairn" / "build" / "m" / "narrative.html").read_text()


def test_a_report_renders_sized_norms_inline_and_in_display(proj):
    page = build(proj, "Inline $\\big\\lVert x \\big\\rVert$ and\n\n$$\n\\Big\\langle a, b \\Big\\rangle\n$$\n")
    assert "\\lVert" not in page.replace('data-latex="', "").split("<math")[1] and "‖" in page and "⟨" in page


def test_a_report_with_unsupported_latex_fails_its_build(proj):
    with pytest.raises((ValueError, SystemExit), match="unsupported LaTeX"):
        build(proj, "Broken $\\notacommand x$.\n")


@pytest.mark.parametrize("latex", [r"\arg\min_z C(q, z)", r"\arg z", r"\hat z(q) = \arg\max_z f"])
def test_arg_is_an_operator(latex):
    mathml = to_mathml(latex, "inline")
    assert "\\" not in mathml and "arg" in mathml


def test_every_standard_operator_name_renders():
    names = (
        "arccos arcsin arctan arg cos cosh cot coth csc deg det dim exp gcd hom inf ker lg lim liminf limsup ln log"
        " max min Pr sec sin sinh sup tan tanh"
    ).split()
    for name in names:
        assert "\\" not in to_mathml(f"\\{name} x", "inline"), name
