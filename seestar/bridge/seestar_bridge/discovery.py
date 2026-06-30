"""3-tier scope-address discovery (preview only).

The telemetry path never depends on knowing a scope's own HTTP address; only the
live stacked-image preview does. We discover ``{device_num: ip_address}`` from,
in priority order:

  1. seestar_alp's ``config.toml`` (bundled/local mode — the file we wrote).
  2. ``{webui}/config.json`` — the machine-readable endpoint
     (smart-underworld/seestar_alp#749), keyed by ``device_num`` directly.
  3. ``{webui}/config`` HTML — scraped form inputs, keyed by ``ss_name`` which we
     match back to each device's Alpaca ``DeviceName`` to recover ``device_num``.

Every source is best-effort: any failure (missing file, connection refused,
non-200, malformed body) falls through to the next tier, and an unresolved scope
is simply absent from the result (its preview is skipped). discover_addresses
never raises.
"""
import json
import re
import urllib.error
import urllib.request

try:  # tomllib is stdlib on >=3.11; tomli is the backport for older runtimes.
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - exercised only on <3.11
    import tomli as _toml

# Document-order pairing of the two relevant config-form inputs. seestar_alp's
# /config page renders one ss_name + one ss_ip_address per scope, in order.
_HTML_INPUT_RE = re.compile(
    r'<input[^>]*name="(ss_name|ss_ip_address)"[^>]*value="([^"]*)"'
)
_HTTP_TIMEOUT_SEC = 5

#: Hard cap on a config response body. The real /config{,.json} payloads are a
#: few KiB; reading past this means the endpoint is not what we expect, so we
#: stop and fall through to the next tier rather than buffer unbounded input.
_MAX_CONFIG_BYTES = 4 * 1024 * 1024

_DEVICE_NAME_KEY = "DeviceName"
_DEVICE_NUMBER_KEY = "DeviceNumber"


def parse_config_toml(text):
    """Parse seestar_alp ``config.toml`` -> ``{device_num: ip_address}``.

    Reads the ``[[seestars]]`` array; each entry carries ``device_num`` and
    ``ip_address``. Entries missing either field are skipped.
    """
    data = _toml.loads(text)
    addresses = {}
    for entry in data.get("seestars", []):
        device_num = entry.get("device_num")
        ip_address = entry.get("ip_address")
        if device_num is None or not ip_address:
            continue
        try:
            addresses[int(device_num)] = ip_address
        except (ValueError, TypeError):
            # Non-numeric device_num in adversarial input -> skip this entry.
            continue
    return addresses


def parse_config_json(payload):
    """Parse the ``/config.json`` payload -> ``{device_num: ip_address}``.

    Expects ``{"devices": [{"device_num": ..., "ip_address": ...}, ...]}``.
    """
    addresses = {}
    for device in payload.get("devices", []):
        device_num = device.get("device_num")
        ip_address = device.get("ip_address")
        if device_num is None or not ip_address:
            continue
        try:
            addresses[int(device_num)] = ip_address
        except (ValueError, TypeError):
            # Non-numeric device_num in adversarial input -> skip this entry.
            continue
    return addresses


def parse_config_html(html):
    """Scrape the ``/config`` HTML form -> ``{ss_name: ss_ip_address}``.

    Pairs consecutive ``ss_name`` / ``ss_ip_address`` inputs in document order;
    a name is only emitted once its following ip_address input is seen.
    """
    addresses = {}
    pending_name = None
    for field, value in _HTML_INPUT_RE.findall(html):
        if field == "ss_name":
            pending_name = value
        elif field == "ss_ip_address" and pending_name is not None:
            addresses[pending_name] = value
            pending_name = None
    return addresses


def _read_toml_file(config_toml_path):
    try:
        with open(config_toml_path, "rb") as handle:
            return parse_config_toml(handle.read().decode())
    except (OSError, ValueError, _toml.TOMLDecodeError):
        # Missing/unreadable file or malformed TOML -> fall through to the webui.
        return {}


def _read_capped(resp):
    """Read up to ``_MAX_CONFIG_BYTES`` + 1 from ``resp``.

    The extra byte lets the caller detect an over-cap body (we read one past the
    limit) without buffering the whole stream. urllib already raises HTTPError on
    a non-200 response, so reaching here means status 200.
    """
    return resp.read(_MAX_CONFIG_BYTES + 1)


def _fetch_config_json(webui_base):
    url = f"{webui_base}/config.json"
    try:
        with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT_SEC) as resp:
            body = _read_capped(resp)
        if len(body) > _MAX_CONFIG_BYTES:
            # Over-cap or garbage body -> fall through to the next tier.
            return {}
        return parse_config_json(json.loads(body.decode()))
    except (urllib.error.URLError, OSError, ValueError):
        # 404 (URLError/HTTPError), connection refused (OSError), or bad JSON.
        return {}


def _fetch_config_html(webui_base, devices):
    url = f"{webui_base}/config"
    try:
        with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT_SEC) as resp:
            body = _read_capped(resp)
        if len(body) > _MAX_CONFIG_BYTES:
            return {}
        name_to_ip = parse_config_html(body.decode())
    except (urllib.error.URLError, OSError, ValueError):
        return {}
    # Match each scraped ss_name back to a device's Alpaca DeviceName to recover
    # the stable device_num; scopes with no matching device are dropped.
    addresses = {}
    for device in devices:
        ip_address = name_to_ip.get(device.get(_DEVICE_NAME_KEY))
        if ip_address:
            addresses[int(device[_DEVICE_NUMBER_KEY])] = ip_address
    return addresses


def discover_addresses(*, config_toml_path, webui_base, devices):
    """Resolve ``{device_num: ip_address}`` for the preview, best-effort.

    Tries the local ``config.toml`` first, then ``{webui}/config.json``, then the
    ``{webui}/config`` HTML scrape (matched against ``devices`` by name). Returns
    the first non-empty source; an empty dict if nothing resolves. Never raises.
    """
    if config_toml_path:
        addresses = _read_toml_file(config_toml_path)
        if addresses:
            return addresses

    if webui_base:
        addresses = _fetch_config_json(webui_base)
        if addresses:
            return addresses
        addresses = _fetch_config_html(webui_base, devices)
        if addresses:
            return addresses

    return {}
