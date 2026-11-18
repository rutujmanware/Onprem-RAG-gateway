"""DEFECT-43: requirements.txt installs the libraries the code imports.

Reported: requirements.txt pinned `jwt>=1.3.1`, which on PyPI is GehirnInc's `jwt`, not PyJWT; a clean install
then failed at `from jwt import PyJWKClient` (askiisc_chat/askiisc/auth.py), so the gateway could not start.
"""

import re
from pathlib import Path

REQ = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text()
NAMES = {re.split(r"[<>=!\[;\s]", l.strip(), 1)[0].lower() for l in REQ.splitlines() if l.strip() and not l.lstrip().startswith("#")}


def test_pyjwt_not_the_unrelated_jwt_package():
    assert "pyjwt" in NAMES
    assert "jwt" not in NAMES, "the PyPI package 'jwt' is not PyJWT and breaks `from jwt import PyJWKClient`"


def test_the_installed_jwt_is_pyjwt():
    from jwt import PyJWKClient  # noqa: F401  (fails with the wrong package)
    import jwt
    assert int(jwt.__version__.split(".")[0]) >= 2
