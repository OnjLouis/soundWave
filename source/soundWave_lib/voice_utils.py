# -*- coding: utf-8 -*-
"""Voice metadata and display-name helpers for soundWave."""

from __future__ import annotations

import re
from typing import List


def normalise_voice_infos(raw) -> List[object]:
    if raw is None:
        return []
    try:
        if isinstance(raw, dict):
            return list(raw.values())
    except Exception:
        pass
    try:
        if hasattr(raw, "values") and callable(raw.values):
            return list(raw.values())
    except Exception:
        pass
    try:
        return list(raw)
    except Exception:
        return []


def looks_numeric_label(value: str) -> bool:
    value = (value or "").strip()
    if not value:
        return False
    if value.isdigit():
        return True
    return bool(re.match(r"^(language|voice)\s+\d+$", value, re.I))


_FRIENDLY_LOCALE_NAMES = {
    "en": "English",
    "en-gb": "UK English",
    "en_gb": "UK English",
    "en-us": "US English",
    "en_us": "US English",
    "es": "Spanish",
    "es-mx": "Mexican Spanish",
    "es_mx": "Mexican Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt-br": "Brazilian Portuguese",
    "pt_br": "Brazilian Portuguese",
    "pt-pt": "Portuguese",
    "pt_pt": "Portuguese",
    "nl": "Dutch",
    "el": "Greek",
    "hu": "Hungarian",
    "hr": "Croatian",
    "ro": "Romanian",
    "cs": "Czech",
    "da": "Danish",
    "sv": "Swedish",
    "nb-no": "Norwegian",
    "nb_no": "Norwegian",
    "pl": "Polish",
    "ms": "Malay",
    "zh": "Chinese",
    "fi": "Finnish",
    "lt": "Lithuanian",
    "cy": "Welsh",
}


def friendly_locale_name(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    key = value.replace("_", "-").lower()
    if key in _FRIENDLY_LOCALE_NAMES:
        return _FRIENDLY_LOCALE_NAMES[key]
    try:
        import languageHandler

        label = languageHandler.getLanguageDescription(value)
        if label:
            return str(label)
    except Exception:
        pass
    return value


def voice_info_text(vi, *names: str) -> str:
    for name in names:
        try:
            value = getattr(vi, name, "")
        except Exception:
            value = ""
        if value is None:
            continue
        value = str(value).strip()
        if value:
            return value
        try:
            value = getattr(vi, name.upper(), "")
        except Exception:
            value = ""
        if value is None:
            continue
        value = str(value).strip()
        if value:
            return value
    return ""


def orpheus_friendly_label(vi, fallback: str = "") -> str:
    """
    Orpheus VoiceInfo objects typically have:
      - name: human language name, e.g. English
      - language: locale code, e.g. en-gb
      - description: sometimes set for variants
    """
    name = voice_info_text(vi, "displayName", "displayNameWithAccelerator", "name")
    locale = voice_info_text(vi, "language", "locale")
    desc = voice_info_text(vi, "description")

    base = desc or name
    friendly_locale = friendly_locale_name(locale)
    if looks_numeric_label(base) and friendly_locale and not looks_numeric_label(friendly_locale):
        base = friendly_locale
    if base and friendly_locale and friendly_locale.lower() not in base.lower() and not looks_numeric_label(base):
        return f"{base} ({friendly_locale})"
    return base or fallback


def voice_choice_label(vi, fallback: str = "") -> str:
    label = voice_info_text(vi, "displayName", "displayNameWithAccelerator", "description", "name")
    locale = voice_info_text(vi, "language", "locale")
    friendly_locale = friendly_locale_name(locale)
    if looks_numeric_label(label) and friendly_locale and not looks_numeric_label(friendly_locale):
        label = friendly_locale
    if label and friendly_locale and friendly_locale.lower() not in label.lower() and not looks_numeric_label(label):
        return f"{label} ({friendly_locale})"
    return label or friendly_locale or fallback
