#!/usr/bin/env python3
"""Read existing lm-sensors / hwmon temperatures; never configure hardware.

Linux hwmon ABI: https://docs.kernel.org/hwmon/sysfs-interface.html
libsensors applies configured labels, conversions and ignored inputs. Raw hwmon
is used only when /usr/bin/sensors isn't installed; its temperature ABI uses
millidegrees Celsius. No sensor discovery, module loading, SMART or fan writes.
"""

import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import selectors
import socket
import ssl
import subprocess
import sys
import threading
import time


MAX_CHANNELS = 256
MAX_TEXT_BYTES = 128
MAX_INPUT_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 262_144
COMMAND_TIMEOUT = 3.0
CACHE_SECONDS = 5.0
REQUEST_TIMEOUT = 5.0
SENSORS_COMMAND = ("/usr/bin/sensors", "-j")
_CHANNEL = re.compile(r"(temp[1-9][0-9]{0,3})_input\Z")
_HWMON = re.compile(r"hwmon[0-9]+\Z")
_NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}\Z")
_NUMBER = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?\Z")


class SampleError(Exception):
    """A fixed error; raw command output and filesystem details stay private."""


def display_text(value):
    if not isinstance(value, str):
        return None
    clean = "".join(char if ord(char) >= 32 and ord(char) != 127 and not 0xD800 <= ord(char) <= 0xDFFF else " " for char in value).strip()
    clean = clean.encode("utf-8")[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore").strip()
    return clean or None


def temperature(value):
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and -100 <= number <= 250 else None


def category(chip):
    name = chip.lower()
    if re.match(r"(?:coretemp|k10temp|zenpower)(?:[-_]|$)", name):
        return "cpu"
    if re.match(r"(?:nvme|drivetemp)(?:[-_]|$)", name):
        return "disk"
    if re.match(r"(?:nct[0-9a-z]*|it8[0-9a-z]*|w836[0-9a-z]*|f718[0-9a-z]*|as[0-9]+|asus[0-9a-z_]*)(?:[-_]|$)", name):
        return "board"
    return "other"


def sensor_id(source, chip_identity, channel):
    identity = json.dumps([source, chip_identity, channel], ensure_ascii=True, separators=(",", ":"))
    return "sensor-" + hashlib.sha256(identity.encode("ascii")).hexdigest()[:24]


def make_sensor(source, identity, chip, label, channel, value, maximum=None, critical=None):
    celsius = temperature(value)
    chip, label = display_text(chip), display_text(label)
    if celsius is None or chip is None or label is None:
        return None
    return {"id": sensor_id(source, identity, channel), "chip": chip, "label": label,
            "category": category(chip), "celsius": celsius, "max_celsius": temperature(maximum),
            "critical_celsius": temperature(critical)}


def parse_sensors_json(raw):
    try:
        if not isinstance(raw, bytes) or len(raw) > MAX_INPUT_BYTES:
            raise SampleError("temperature_read_failed")
        data = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise SampleError("temperature_read_failed") from None
    if not isinstance(data, dict):
        raise SampleError("temperature_read_failed")
    result, seen = [], set()
    for chip in sorted(data):
        features = data[chip]
        if not isinstance(features, dict) or not display_text(chip):
            continue
        for label in sorted(features):
            values = features[label]
            if not isinstance(values, dict):
                continue
            for key in sorted(values):
                match = _CHANNEL.fullmatch(key)
                if match is None:
                    continue
                channel = match[1]
                # A fault flag is distinct from an over-temperature alarm. Keep
                # actual alarm readings, but never display a faulted sensor.
                if channel + "_fault" in values and (type(values[channel + "_fault"]) not in (int, float)
                        or not math.isfinite(values[channel + "_fault"]) or values[channel + "_fault"] != 0):
                    continue
                if values.get(channel + "_enable") == 0:
                    continue
                row = make_sensor("sensors", chip, chip, label, channel, values[key],
                                  values.get(channel + "_max"), values.get(channel + "_crit"))
                if row is not None and row["id"] not in seen:
                    result.append(row)
                    seen.add(row["id"])
                    if len(result) == MAX_CHANNELS:
                        return result
    return result


def read_sensors_output():
    """Bound both wall-clock time and captured stdout, without a shell."""
    try:
        process = subprocess.Popen(SENSORS_COMMAND, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, shell=False, close_fds=True)
    except FileNotFoundError:
        raise
    except OSError:
        raise SampleError("temperature_read_failed") from None
    output = bytearray()
    deadline = time.monotonic() + COMMAND_TIMEOUT
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SampleError("temperature_read_failed")
                if not selector.select(remaining):
                    raise SampleError("temperature_read_failed")
                chunk = os.read(process.stdout.fileno(), min(65_536, MAX_INPUT_BYTES + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > MAX_INPUT_BYTES:
                    raise SampleError("temperature_read_failed")
        remaining = deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) != 0:
            raise SampleError("temperature_read_failed")
    except (OSError, subprocess.SubprocessError):
        raise SampleError("temperature_read_failed") from None
    finally:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        if process.stdout is not None:
            process.stdout.close()
    return bytes(output)


def read_attribute(path):
    try:
        with path.open("rb") as handle:
            raw = handle.read(513)
        if len(raw) > 512:
            return None
        return raw.decode("utf-8").strip()
    except (OSError, UnicodeError):
        return None


def hwmon_number(path):
    value = read_attribute(path)
    if value is None or _NUMBER.fullmatch(value) is None:
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def physical_chip_identity(path, name):
    try:
        physical = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    # hwmon numbering is assigned at enumeration time, not a hardware identity.
    # Retain the physical device path, including e.g. PCI address / coretemp.0.
    if _HWMON.fullmatch(physical.name):
        physical = physical.parent
        if physical.name == "hwmon":
            physical = physical.parent
    return str(physical) + "\0" + name


def read_hwmon(root=Path("/sys/class/hwmon")):
    try:
        chips = sorted(path for path in root.iterdir() if _HWMON.fullmatch(path.name))
    except FileNotFoundError:
        return []
    except OSError:
        raise SampleError("temperature_read_failed") from None
    result, seen, attempted = [], set(), 0
    for chip_path in chips[:256]:
        name = read_attribute(chip_path / "name")
        if not name:
            continue
        identity = physical_chip_identity(chip_path, name)
        if identity is None:
            continue
        try:
            inputs = sorted(path for path in chip_path.iterdir() if _CHANNEL.fullmatch(path.name))
        except OSError:
            continue
        for path in inputs:
            attempted += 1
            if attempted > 2048:
                return result
            channel = _CHANNEL.fullmatch(path.name)[1]
            fault_path = chip_path / (channel + "_fault")
            fault = hwmon_number(fault_path)
            if fault != 0 and (fault is not None or fault_path.exists()):
                continue
            enabled = hwmon_number(chip_path / (channel + "_enable"))
            if enabled == 0:
                continue
            value = hwmon_number(path)
            if value is None:
                continue
            maximum = hwmon_number(chip_path / (channel + "_max"))
            critical = hwmon_number(chip_path / (channel + "_crit"))
            row = make_sensor("hwmon", identity, name, read_attribute(chip_path / (channel + "_label")) or channel,
                              channel, value / 1000, maximum / 1000 if maximum is not None else None,
                              critical / 1000 if critical is not None else None)
            if row is not None and row["id"] not in seen:
                result.append(row)
                seen.add(row["id"])
                if len(result) == MAX_CHANNELS:
                    return result
    return result


def read_temperatures():
    try:
        raw = read_sensors_output()
    except FileNotFoundError:
        return read_hwmon()
    return parse_sensors_json(raw)


class TemperatureSampler:
    def __init__(self, node, *, reader=None, monotonic=time.monotonic, clock=time.time):
        self.node, self.reader, self.monotonic, self.clock = node, reader or read_temperatures, monotonic, clock
        self._lock = threading.Lock()
        self._cached, self._expires, self._error_until = None, 0, 0

    def get(self):
        with self._lock:
            now = self.monotonic()
            if self._cached is not None and now < self._expires:
                return self._cached
            if now < self._error_until:
                raise SampleError("temperature_read_failed")
            try:
                result = {"version": 1, "node": self.node, "sampled_at": int(self.clock()), "sensors": self.reader()}
                # The raw source and text bounds make this a defensive limit,
                # not a reason to silently drop channels from a valid sample.
                encode_json(result)
            except (SampleError, OSError, ValueError, TypeError, OverflowError):
                self._error_until = self.monotonic() + CACHE_SECONDS
                raise SampleError("temperature_read_failed") from None
            self._cached, self._expires = result, self.monotonic() + CACHE_SECONDS
            return result


def encode_json(value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_RESPONSE_BYTES:
        raise SampleError("temperature_read_failed")
    return data


def load_token(path):
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(4097)
        if len(raw) > 4096:
            raise ValueError()
        token = raw.decode("ascii").strip()
    except (OSError, UnicodeError, ValueError):
        raise ValueError("cannot read a valid token file") from None
    if not 32 <= len(token) <= 256 or any(ord(char) <= 32 or ord(char) >= 127 for char in token):
        raise ValueError("token file must contain 32 to 256 non-whitespace ASCII characters")
    return token.encode("ascii")


class TemperatureHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, sampler, token):
        self.sampler, self.token = sampler, token
        self._slots = threading.BoundedSemaphore(32)
        if ipaddress.ip_address(address[0]).version == 6:
            self.address_family = socket.AF_INET6
        super().__init__(address, TemperatureHandler)

    def enable_tls(self, context):
        # Defer handshake to the bounded worker socket read, so an idle TLS
        # client cannot block the accept loop before its timeout is installed.
        self.socket = context.wrap_socket(self.socket, server_side=True, do_handshake_on_connect=False)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        # The default prints exception tracebacks that could contain request
        # data. Sampling errors have fixed HTTP responses instead.
        pass


class TemperatureHandler(BaseHTTPRequestHandler):
    server_version = "pve-temperature-collector/1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT)

    def log_message(self, format, *args):
        # Never log raw URLs, Authorization headers or tokens.
        pass

    def respond(self, status, value):
        body = encode_json(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        if status == 405:
            self.send_header("Allow", "GET")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        headers = self.headers.get_all("Authorization", [])
        supplied = headers[0].split(" ", 1) if len(headers) == 1 else []
        authorized = len(supplied) == 2 and supplied[0].lower() == "bearer"
        if not authorized or not hmac.compare_digest(supplied[1].encode("latin-1"), self.server.token):
            self.respond(401, {"error": "unauthorized"})
            return
        if self.path != "/v1/temperatures":
            self.respond(404, {"error": "not_found"})
            return
        try:
            self.respond(200, self.server.sampler.get())
        except SampleError:
            self.respond(503, {"error": "temperature_read_failed"})

    def method_not_allowed(self):
        self.respond(405, {"error": "method_not_allowed"})

    do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = method_not_allowed


def tls_context(certfile, keyfile):
    if not certfile or not keyfile:
        raise ValueError("certfile and keyfile must be provided together")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile, keyfile)
    return context


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True, help="Exact PVE node name returned in every sample")
    parser.add_argument("--listen", default="127.0.0.1", help="Bind IP address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9469)
    parser.add_argument("--token-file", help="Bearer token file, 32 to 256 ASCII characters")
    parser.add_argument("--certfile", help="Optional TLS certificate PEM")
    parser.add_argument("--keyfile", help="Optional TLS private key PEM")
    parser.add_argument("--once", action="store_true", help="Print one JSON sample; no token or HTTP listener needed")
    return parser


def main(argv=None):
    parser = argument_parser()
    args = parser.parse_args(argv)
    if _NODE.fullmatch(args.node) is None:
        parser.error("node must be a PVE short node name of 1 to 64 ASCII letters, digits or hyphens")
    if bool(args.certfile) != bool(args.keyfile):
        parser.error("certfile and keyfile must be provided together")
    sampler = TemperatureSampler(args.node)
    if args.once:
        try:
            print(encode_json(sampler.get()).decode("utf-8"))
            return 0
        except SampleError:
            print("temperature read failed", file=sys.stderr)
            return 1
    if not args.token_file:
        parser.error("token-file is required unless once is selected")
    try:
        ipaddress.ip_address(args.listen)
        if not 1 <= args.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        token = load_token(args.token_file)
        context = tls_context(args.certfile, args.keyfile) if args.certfile else None
    except (ValueError, OSError, ssl.SSLError):
        parser.error("invalid listener, token file or TLS configuration")
    try:
        with TemperatureHTTPServer((args.listen, args.port), sampler, token) as server:
            if context is not None:
                server.enable_tls(context)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    except OSError:
        print("temperature collector could not start", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
