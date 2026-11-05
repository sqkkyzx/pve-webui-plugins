"""Validate display-only module icon specifications without fetching them."""

import ipaddress
import re
from urllib.parse import urlsplit


NATIVE_ICON = re.compile(r"(?:fa fa-[a-z0-9-]+|fl fl-[a-z0-9-]+)\Z")
HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


def icon_url(value):
    """Accept an absolute HTTP(S) image URL and retain its literal spelling."""
    if (not isinstance(value, str) or not 1 <= len(value) <= 2048
            or any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159
                   or 0xD800 <= ord(char) <= 0xDFFF or char == "\\" for char in value)):
        raise ValueError("Invalid icon URL")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.netloc.endswith(":")):
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
        hostname = parsed.hostname
        if "%" in hostname:
            raise ValueError
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            hostname = hostname.encode("idna").decode("ascii").rstrip(".")
            if (not hostname or len(hostname) > 253
                    or any(HOST_LABEL.fullmatch(label) is None for label in hostname.split("."))):
                raise ValueError
        else:
            if address.version == 6 and not parsed.netloc.startswith("["):
                raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError("Invalid icon URL") from None
    return value


def validate_icon(specification):
    if (not isinstance(specification, dict) or set(specification) != {"type", "value"}
            or not isinstance(specification["type"], str) or not isinstance(specification["value"], str)):
        raise ValueError("Invalid icon")
    kind, content = specification["type"], specification["value"]
    if kind == "pve":
        if NATIVE_ICON.fullmatch(content) is None:
            raise ValueError("Invalid native icon")
    elif kind == "url":
        icon_url(content)
    else:
        raise ValueError("Invalid icon type")
    return {"type": kind, "value": content}


def validate_module_icons(value):
    if not isinstance(value, dict) or set(value) - {"komodo", "temperatures", "safeline"}:
        raise ValueError("Invalid module icons")
    return {module_id: validate_icon(specification) for module_id, specification in value.items()}
