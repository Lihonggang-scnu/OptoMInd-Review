"""Portable disk components for opaque task identities (never change JSON IDs)."""

from hashlib import sha256
import re


def portable_component(value: str) -> str:
    """Keep safe legacy names; hash unsafe names into a reserved namespace.

    This is a filename component, not a whole-path sanitizer. Windows device
    names, ADS, separators, controls, trailing dots/spaces, and oversized names
    are excluded on every platform. The reserved prefix prevents a literal safe
    ID from aliasing an encoded one (also under Windows case folding). A 96-bit
    SHA-256 suffix distinguishes lossy slugs without inflating common paths.
    Caller-selected output roots and overall Windows path length are unchanged.
    """
    prefix = "~id-"
    reserved = re.fullmatch(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])",
                            value.split(".", 1)[0].rstrip(" "), re.IGNORECASE)
    safe = (bool(value) and value not in {".", ".."}
            and not value.casefold().startswith(prefix) and not reserved
            and not re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', value)
            and not value.endswith((".", " "))
            and len(value.encode("utf-8")) <= 120)
    if safe:
        return value
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")[:24] or "unit"
    return f"{prefix}{slug}-{sha256(value.encode('utf-8')).hexdigest()[:24]}"
