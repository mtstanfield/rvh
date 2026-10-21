"""evals/public/ ships with rvh, so it may hold only public-advisory labels."""
from pathlib import Path

from eval_corpus import check_public, load_corpus

PUBLIC = Path(__file__).resolve().parent.parent / "evals" / "public"


def test_the_public_corpus_loads_and_passes_the_export_guard():
    assert check_public(load_corpus(PUBLIC)) == []


def test_the_families_map_groups_sibling_cwes():
    corpus = load_corpus(PUBLIC)
    for siblings in (("CWE-77", "CWE-78", "CWE-88"), ("CWE-94", "CWE-95", "CWE-1336"),
                     ("CWE-285", "CWE-639", "CWE-862", "CWE-863"), ("CWE-502", "CWE-915"),
                     ("CWE-120", "CWE-125", "CWE-787")):
        assert len({corpus.family_of(cwe) for cwe in siblings}) == 1, siblings
