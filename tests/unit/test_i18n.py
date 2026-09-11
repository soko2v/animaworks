"""Unit tests for core/i18n.py."""
from __future__ import annotations

import pytest



class TestTranslationFunction:
    """Tests for the t() function."""

    def test_returns_ja_string_for_ja_locale(self):
        """t() returns Japanese string when locale is 'ja'."""
        from core.i18n import t
        result = t("anima.status_idle", locale="ja")
        assert result == "待機中"

    def test_returns_en_string_for_en_locale(self):
        """t() returns English string when locale is 'en'."""
        from core.i18n import t
        result = t("anima.status_idle", locale="en")
        assert result == "Idle"

    def test_fallback_to_en_for_unknown_locale(self):
        """t() falls back to 'ja' for unrecognized locales."""
        from core.i18n import t
        result = t("anima.status_idle", locale="fr")
        # Should fall back to ja since fr is not in ("ja", "en", "zh", "ko")
        assert result == "待機中"

    def test_returns_key_for_missing_entry(self):
        """t() returns the key itself when no translation exists."""
        from core.i18n import t
        result = t("nonexistent.key", locale="ja")
        assert result == "nonexistent.key"

    def test_placeholder_substitution(self):
        """t() substitutes {placeholder} values."""
        from core.i18n import t
        result = t("handler.anima_not_found", locale="en", target_name="TestAnima")
        assert "TestAnima" in result
        assert "{target_name}" not in result

    def test_missing_placeholder_preserved(self):
        """t() preserves {placeholder} when value not provided."""
        from core.i18n import t
        result = t("handler.anima_not_found", locale="en")
        assert "{target_name}" in result

    def test_fallback_chain_en_then_ja(self):
        """When locale entry missing, falls back en → ja."""
        from core.i18n import t, _STRINGS
        # Temporarily add a key with only ja
        _STRINGS["_test_only_ja"] = {"ja": "日本語のみ"}
        try:
            result = t("_test_only_ja", locale="en")
            assert result == "日本語のみ"
        finally:
            del _STRINGS["_test_only_ja"]

    def test_all_keys_have_ja_and_en(self):
        """Every key in _STRINGS has both 'ja' and 'en' entries."""
        from core.i18n import _STRINGS
        missing = []
        for key, translations in _STRINGS.items():
            if "ja" not in translations:
                missing.append(f"{key}: missing 'ja'")
            if "en" not in translations:
                missing.append(f"{key}: missing 'en'")
        assert not missing, "Missing translations:\n" + "\n".join(missing)

    def test_no_empty_translations(self):
        """No translation value should be empty string."""
        from core.i18n import _STRINGS
        empty = []
        for key, translations in _STRINGS.items():
            for lang, value in translations.items():
                if not value.strip():
                    empty.append(f"{key}[{lang}]")
        assert not empty, f"Empty translations: {empty}"


class TestSafeFormatDict:
    """Tests for _SafeFormatDict."""

    def test_missing_key_returns_braced(self):
        from core.i18n import _SafeFormatDict
        d = _SafeFormatDict({"a": "1"})
        result = "hello {a} {b}".format_map(d)
        assert result == "hello 1 {b}"


@pytest.mark.parametrize("key,kwargs", [
    ("chat.image_file_too_large", {"size_mb": 6}),
    ("chat.invalid_image_data", {}),
])
def test_image_validation_messages_have_korean_translations(key, kwargs):
    from string import Formatter

    from core.i18n import _STRINGS, t

    translations = _STRINGS[key]
    assert "ko" in translations
    def fields(text):
        return {name for _, name, _, _ in Formatter().parse(text) if name}
    assert fields(translations["ko"]) == fields(translations["en"]) == fields(translations["ja"])
    assert t(key, locale="ko", **kwargs) == translations["ko"].format(**kwargs)
