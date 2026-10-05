import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_sample_docs_module():
    spec = importlib.util.spec_from_file_location("make_sample_docs", ROOT / "dev" / "make_sample_docs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def sample_docs(tmp_path_factory) -> Path:
    """dev/make_sample_docs.py output in a temp folder: PO Tracker.xlsx, supplier PDFs, a scan, docx, pptx."""
    out = tmp_path_factory.mktemp("sample-docs")
    _load_sample_docs_module().make_all(out)
    return out
