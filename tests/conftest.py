import pytest

from app import db, seed
from app.agent import go_no_go


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv("META_MODEL", "muse-spark-1.3")
    monkeypatch.setattr(seed, "PRIVATE_PROFILE", tmp_path / "no_private_profile.json")
    db.init_db()
    return tmp_path / "test.sqlite3"


def fake_pp(verified: int):
    """Stand-in for matrix.assess_past_performance returning `verified` verified matches."""
    entry = {"client": "C", "scope": "S", "verified": True}
    return lambda firm, sol, llm: {"verified": [{"client": "C", "quote": "S", "entry": entry}] * verified, "size": ""}


@pytest.fixture(autouse=True)
def three_verified_matches(monkeypatch):
    """Go/no-go tests that are not about the floor see enough matches to leave the model verdict alone."""
    monkeypatch.setattr(go_no_go, "assess_past_performance", fake_pp(3))


def rows(sql, params=()):
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def make_pdf(lines: list[str]) -> bytes:
    """Minimal single-page text PDF (Helvetica) that pypdf can extract."""

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    content = "BT /F1 10 Tf 12 TL 40 760 Td " + " T* ".join(f"({esc(line)}) Tj" for line in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


SAMPLE_TEXT = """\
Solicitation 36C10X26Q0042
Offers are due October 30, 2026 at 2:00 PM Eastern.
NAICS Code: 541512
This acquisition is a HUBZone set-aside.
Place of Performance: Syracuse, NY
SECTION C - DESCRIPTION
The contractor shall provide software modernization services.
SECTION D - PACKAGING
n/a
SECTION L - INSTRUCTIONS TO OFFERORS
Submit a technical volume and a price volume.
SECTION M - EVALUATION FACTORS
Technical approach, past performance, price.
"""
