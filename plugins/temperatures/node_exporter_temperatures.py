"""Join a bounded allowlist of node_exporter hwmon samples into sensor DTOs.

The v1.9 collector emits temperature values in Celsius and annotations with
value 1. Its temp_enable_celsius flag is scaled by 0.001; fault is unscaled.
Source: prometheus/node_exporter v1.9.0 collector/hwmon_linux.go.
This parses one scrape, not PromQL, histories, histograms or arbitrary metrics.
"""

import hashlib
import json
import math
import re


_MAX_BYTES = 4_194_304
_MAX_MATCHES = 4096
_MAX_SENSORS = 256
_MAX_TEXT = 128
_NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}\Z")
_NAME = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
_LABEL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SENSOR = re.compile(r"temp[1-9][0-9]{0,3}\Z")
_NUMBER = re.compile(r"(?:[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|[+-]?(?:Inf(?:inity)?|NaN))\Z", re.IGNORECASE)
_TIMESTAMP = re.compile(r"[+-]?[0-9]{1,32}\Z")
_FIELDS = {
    "node_hwmon_temp_celsius": "celsius",
    "node_hwmon_temp_max_celsius": "maximum",
    "node_hwmon_temp_crit_celsius": "critical",
    "node_hwmon_temp_fault": "fault",
    # v1.9's general temperature-unit branch also scales tempN_enable.
    "node_hwmon_temp_enable_celsius": "scaled_enable",
    # Unscaled spelling is accepted for exporters exposing the flag directly.
    "node_hwmon_temp_enable": "enable",
}
_METADATA = {"node_hwmon_chip_names", "node_hwmon_sensor_label"}
_SUCCESS = "node_scrape_collector_success"
_ALLOWED = set(_FIELDS) | _METADATA | {_SUCCESS}


def _invalid():
    raise ValueError("invalid_node_exporter_response")


def _text(value, *, empty=False):
    if (len(value) > _MAX_TEXT or any(ord(char) < 32 and char != "\n" or ord(char) == 127
            or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        _invalid()
    # The shared sensor DTO forbids control characters. Preserve an escaped
    # newline visibly rather than silently joining its two words or dropping it.
    value = value.replace("\n", "\\n")
    if len(value) > _MAX_TEXT or not empty and not value.strip():
        _invalid()
    return value


def _quoted(text, position):
    if position >= len(text) or text[position] != '"':
        _invalid()
    position += 1
    value = []
    while position < len(text):
        char = text[position]
        position += 1
        if char == '"':
            return "".join(value), position
        if char == "\\":
            if position == len(text) or text[position] not in {"n", "\\", '"'}:
                _invalid()
            value.append({"n": "\n", "\\": "\\", '"': '"'}[text[position]])
            position += 1
        else:
            if ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF:
                _invalid()
            value.append(char)
        if len(value) > _MAX_TEXT:
            _invalid()
    _invalid()


def _labels(text):
    if not text.startswith("{"):
        _invalid()
    labels, position = {}, 1
    while True:
        while position < len(text) and text[position] in " \t":
            position += 1
        if position < len(text) and text[position] == "}":
            return labels, position + 1
        match = _LABEL_NAME.match(text, position)
        if match is None or match[0] in labels:
            _invalid()
        key, position = match[0], match.end()
        while position < len(text) and text[position] in " \t":
            position += 1
        if position >= len(text) or text[position] != "=":
            _invalid()
        position += 1
        while position < len(text) and text[position] in " \t":
            position += 1
        value, position = _quoted(text, position)
        labels[key] = value
        if len(labels) > 3:
            _invalid()
        while position < len(text) and text[position] in " \t":
            position += 1
        if position < len(text) and text[position] == "}":
            return labels, position + 1
        if position >= len(text) or text[position] != ",":
            _invalid()
        position += 1


def _sample(line, name, start):
    labels, position = _labels(line[start:])
    rest = line[start + position:]
    if not rest or rest[0] not in " \t":
        _invalid()
    parts = rest.split()
    if (len(parts) not in {1, 2} or _NUMBER.fullmatch(parts[0]) is None
            or len(parts) == 2 and _TIMESTAMP.fullmatch(parts[1]) is None):
        _invalid()
    try:
        value = float(parts[0])
    except (ValueError, OverflowError):
        _invalid()
    expected = {"collector"} if name == _SUCCESS else ({"chip", "chip_name"} if name == "node_hwmon_chip_names" else (
        {"chip", "sensor", "label"} if name == "node_hwmon_sensor_label" else {"chip", "sensor"}))
    if set(labels) != expected:
        _invalid()
    # Bound every metadata string, including fan labels which won't be emitted.
    labels = {key: _text(value, empty=key in {"label", "chip_name"}) for key, value in labels.items()}
    return labels, value


def _category(name):
    name = name.lower()
    if re.match(r"(?:coretemp|k10temp|zenpower)(?:[_-]|$)", name):
        return "cpu"
    if re.match(r"(?:nvme|drivetemp)(?:[_-]|$)", name):
        return "disk"
    if re.match(r"(?:nct[0-9a-z_]*|it8[0-9a-z_]*|w836[0-9a-z_]*|f718[0-9a-z_]*|asus[0-9a-z_]*)(?:[_-]|$)", name):
        return "board"
    return "other"


def _chip_display(identifier, name):
    if not name or name == identifier:
        return identifier
    budget = _MAX_TEXT - len(identifier) - 3
    return name[:budget] + " [" + identifier + "]" if budget > 0 else identifier


def _temperature(value):
    return value if value is not None and math.isfinite(value) and -100 <= value <= 250 else None


def parse_node_exporter_temperatures(text, node, sampled_at):
    """Parse one trusted-origin scrape; malformed allowlisted data fails closed."""
    if (not isinstance(text, str) or not isinstance(node, str) or _NODE.fullmatch(node) is None
            or type(sampled_at) not in {int, float}):
        _invalid()
    try:
        if len(text) > _MAX_BYTES or len(text.encode("utf-8")) > _MAX_BYTES or not 0 <= sampled_at <= 9_007_199_254_740_991 or not math.isfinite(sampled_at):
            _invalid()
    except (UnicodeError, OverflowError):
        _invalid()
    chips, labels, channels, matches, input_count = {}, {}, {}, 0, 0
    has_hwmon, success_seen = False, False
    for raw_line in text.split("\n"):
        line = raw_line.rstrip("\r").lstrip(" \t")
        if not line or line.startswith("#"):
            continue
        match = _NAME.match(line)
        if match is None or match[0] not in _ALLOWED:
            continue
        matches += 1
        if matches > _MAX_MATCHES:
            _invalid()
        name = match[0]
        meta, value = _sample(line, name, match.end())
        if name == _SUCCESS:
            if meta["collector"] != "hwmon":
                continue
            if success_seen or not math.isfinite(value) or value != 1:
                _invalid()
            success_seen, has_hwmon = True, True
            continue
        has_hwmon = True
        chip = meta["chip"]
        if name in _METADATA and (not math.isfinite(value) or value != 1):
            _invalid()
        if name == "node_hwmon_chip_names":
            if chip in chips:
                _invalid()
            chips[chip] = meta["chip_name"]
            continue
        sensor = meta["sensor"]
        if name == "node_hwmon_sensor_label":
            key = chip, sensor
            if key in labels:
                _invalid()
            labels[key] = meta["label"]
            continue
        if _SENSOR.fullmatch(sensor) is None:
            _invalid()
        key = chip, sensor
        values = channels.setdefault(key, {})
        field = _FIELDS[name]
        if field in values or field in {"enable", "scaled_enable"} and ("enable" in values or "scaled_enable" in values):
            _invalid()
        values[field] = value
        if field == "celsius":
            input_count += 1
            if input_count > _MAX_SENSORS:
                _invalid()
    if not has_hwmon:
        _invalid()
    result = []
    for (chip, sensor), values in sorted(channels.items()):
        celsius = _temperature(values.get("celsius"))
        if celsius is None:
            continue
        fault = values.get("fault", 0)
        if not math.isfinite(fault) or fault != 0:
            continue
        if "scaled_enable" in values and values["scaled_enable"] != 0.001:
            continue
        if "enable" in values and values["enable"] != 1:
            continue
        name = chips.get(chip, "")
        identity = json.dumps([chip, sensor], ensure_ascii=True, separators=(",", ":"))
        result.append({"id": "node-hwmon:" + hashlib.sha256(identity.encode("ascii")).hexdigest()[:32],
            "chip": _chip_display(chip, name), "label": labels.get((chip, sensor)) or sensor,
            # A sensor number or label is never evidence of a CPU chip.
            "category": _category(name), "celsius": celsius,
            "max_celsius": _temperature(values.get("maximum")), "critical_celsius": _temperature(values.get("critical"))})
    return {"version": 1, "node": node, "sampled_at": sampled_at, "sensors": result}
