import json
from pathlib import Path

import pytest

from services.bot import i18n

LOCALES_DIR = Path(__file__).parent.parent.parent / "services" / "bot" / "locales"


@pytest.fixture(autouse=True)
def _reset_i18n_overrides():
    """i18n._overrides is deliberately plain module-level, mutable, shared
    process state (see i18n.py's own comment on why t() can't just await a
    DB call) -- every test in this file that sets one must not leak it into
    a sibling test (or a wholly different test file sharing this same
    pytest process) that assumes the pure file-based default.
    """
    yield
    i18n.set_overrides({})


def test_am_and_en_have_matching_key_sets():
    am_keys = set(json.loads((LOCALES_DIR / "am.json").read_text(encoding="utf-8")))
    en_keys = set(json.loads((LOCALES_DIR / "en.json").read_text(encoding="utf-8")))
    assert am_keys == en_keys, (
        f"am/en key sets differ: only in am={am_keys - en_keys}, only in en={en_keys - am_keys}"
    )


def test_default_language_is_amharic():
    assert i18n.DEFAULT_LANGUAGE == "am"
    assert i18n.resolve_language(None) == "am"


def test_unsupported_language_falls_back_to_default():
    assert i18n.resolve_language("fr") == "am"


def test_lookup_in_amharic():
    assert i18n.t("register.prompt", "am") == (
        "ዘመን ጌም ዕድሜያቸው 18 እና ከዚያ በላይ ለሆኑ ተጫዋቾች ብቻ ነው። "
        "ለመመዝገብ ስልክ ቁጥርዎን ያጋሩ - ይህንን በማድረግዎ ዕድሜዎ ቢያንስ 18 መሆኑን ያረጋግጣሉ።"
    )


def test_lookup_in_english():
    assert i18n.t("register.use_button", "en") == "Please use the 'Share Phone Number' button."


def test_formatting_placeholders():
    result = i18n.t("register.success", "en", name="Nebyu")
    assert "Nebyu" in result


def test_om_falls_back_to_english_for_missing_keys():
    # om.json is a deliberate stub -- most keys aren't there yet.
    assert i18n.t("register.prompt", "om") == i18n.t("register.prompt", "en")


def test_om_has_its_own_value_for_keys_it_does_define():
    value = i18n.t("language.set", "om")
    assert value != i18n.t("language.set", "en")


def test_missing_key_raises():
    with pytest.raises(KeyError):
        i18n.t("this.key.does.not.exist", "en")


def test_all_keys_returns_amharic_key_set():
    keys = i18n.all_keys()
    assert "register.prompt" in keys
    assert "wallet.insufficient" in keys


def test_default_template_ignores_any_override():
    original_default = i18n.default_template("menu.play", "am")
    i18n.set_overrides({("menu.play", "am"): "Custom Play Label"})
    # t() now returns the override, but default_template() -- "what ships
    # in the repo," the baseline a Bot Content admin edit is compared and
    # reset against -- must stay exactly what it was before the override.
    assert i18n.default_template("menu.play", "am") == original_default
    assert i18n.t("menu.play", "am") == "Custom Play Label"


def test_override_takes_priority_over_the_shipped_default():
    default = i18n.default_template("menu.play", "am")
    i18n.set_overrides({("menu.play", "am"): "Custom Play Label"})
    assert i18n.t("menu.play", "am") == "Custom Play Label"
    assert i18n.t("menu.play", "en") == i18n.default_template("menu.play", "en")  # untouched
    assert default != "Custom Play Label"


def test_override_still_formats_placeholders():
    i18n.set_overrides({("register.success", "en"): "Welcome aboard, {name}!"})
    assert i18n.t("register.success", "en", name="Nebyu") == "Welcome aboard, Nebyu!"


def test_required_placeholders_extracts_named_fields():
    assert i18n.required_placeholders("Hi {name}, you have {amount} ETB") == frozenset({"name", "amount"})
    assert i18n.required_placeholders("No placeholders here") == frozenset()
