import re

from ansible.errors import AnsibleFilterError

_SERIAL_MODULUS = 1 << 32
_SERIAL_PATTERN = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?P<value>[0-9]+)"
    r"(?P<suffix>[ \t]*;[ \t]*serial[ \t]*)$"
)


def _serial(value, source):
    if isinstance(value, int) and not isinstance(value, bool):
        serial = value
    elif isinstance(value, str) and value.isdecimal():
        serial = int(value)
    else:
        raise AnsibleFilterError(f"{source} must be an unsigned 32-bit integer")

    if not 0 <= serial < _SERIAL_MODULUS:
        raise AnsibleFilterError(f"{source} must be an unsigned 32-bit integer")

    return serial


def _zone_serial_and_normalized_content(content, source):
    if not isinstance(content, str):
        raise AnsibleFilterError(f"{source} must be text")

    matches = list(_SERIAL_PATTERN.finditer(content))
    if len(matches) != 1:
        raise AnsibleFilterError(
            f"{source} must contain exactly one '<number> ; serial' line"
        )

    serial = _serial(matches[0].group("value"), f"{source} SOA serial")
    normalized_content = _SERIAL_PATTERN.sub(
        r"\g<indent>__SOA_SERIAL__\g<suffix>", content, count=1
    )
    return serial, normalized_content


def dns_authoritative_soa_serial(existing_content, desired_content, initial_serial=1):
    """Keep the current serial, or advance it once when zone content changes."""
    initial_serial = _serial(initial_serial, "initial SOA serial")
    _, desired_normalized = _zone_serial_and_normalized_content(
        desired_content, "desired zone content"
    )

    if existing_content is None:
        return initial_serial

    existing_serial, existing_normalized = _zone_serial_and_normalized_content(
        existing_content, "existing zone content"
    )
    if existing_normalized == desired_normalized:
        return existing_serial

    return (existing_serial + 1) % _SERIAL_MODULUS


class FilterModule:
    def filters(self):
        return {"dns_authoritative_soa_serial": dns_authoritative_soa_serial}
