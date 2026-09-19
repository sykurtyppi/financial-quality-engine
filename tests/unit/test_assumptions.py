"""Standing assumptions: parsed leniently, appended safely, handed to the
brief as a numbered data file, and their absence is a diagnostic."""

from __future__ import annotations

import pytest

from app.services.brief import assumptions as asm


class TestParse:
    def test_bullets_numbers_and_checkboxes_in_order(self):
        text = ("# NVDA — standing assumptions\n# comment\n\n"
                "- DC revenue keeps growing  >50% YoY\n"
                "* Gross margin stays above 70%\n"
                "2) Buybacks offset SBC\n"
                "- [ ] No new China restrictions\n"
                "prose line that is not an assumption\n")
        assert asm.parse_assumptions(text) == [
            "DC revenue keeps growing >50% YoY", "Gross margin stays above 70%",
            "Buybacks offset SBC", "No new China restrictions"]

    def test_bounded(self):
        assert asm.parse_assumptions("- " + "x" * 1000)[0] == "x" * asm.MAX_ASSUMPTION_CHARS
        many = "\n".join(f"- a{i}" for i in range(30))
        assert len(asm.parse_assumptions(many)) == asm.MAX_ASSUMPTIONS


class TestAddAndLoad:
    def test_add_creates_header_appends_and_dedups(self, tmp_path):
        p = asm.add_assumption("nvda", "  DC revenue   keeps growing ", root=tmp_path)
        assert p == tmp_path / "NVDA.md" and p.read_text().startswith("# NVDA")
        asm.add_assumption("NVDA", "Buybacks offset SBC", root=tmp_path)
        asm.add_assumption("NVDA", "DC revenue keeps growing", root=tmp_path)  # duplicate
        assert asm.load_assumptions("NVDA", root=tmp_path) == [
            "DC revenue keeps growing", "Buybacks offset SBC"]

    def test_rejects_empty_and_thesis_length_and_overflow(self, tmp_path):
        with pytest.raises(ValueError):
            asm.add_assumption("NVDA", "   ", root=tmp_path)
        with pytest.raises(ValueError, match="thesis"):
            asm.add_assumption("NVDA", "y" * 301, root=tmp_path)
        for i in range(asm.MAX_ASSUMPTIONS):
            asm.add_assumption("NVDA", f"a{i}", root=tmp_path)
        with pytest.raises(ValueError, match="retire"):
            asm.add_assumption("NVDA", "one more", root=tmp_path)

    def test_missing_file_is_empty_and_ticker_is_validated(self, tmp_path):
        assert asm.load_assumptions("NVDA", root=tmp_path) == []
        with pytest.raises(ValueError):
            asm.assumptions_path("../etc", root=tmp_path)


def test_render_numbers_them():
    out = asm.render_for_brief("NVDA", ["a", "b"])
    assert "1. a\n2. b\n" in out and "holder-authored" in out
