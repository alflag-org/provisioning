from ansible.errors import AnsibleFilterError


_SERIAL_MODULUS = 1 << 32
_SERIAL_HALF_RANGE = _SERIAL_MODULUS // 2


def _serial(value, position):
    if isinstance(value, int) and not isinstance(value, bool):
        serial = value
    elif isinstance(value, str) and value.isdecimal():
        serial = int(value)
    else:
        raise AnsibleFilterError(
            f"authoritative DNS serial {position} must be an unsigned 32-bit integer"
        )

    if not 0 <= serial < _SERIAL_MODULUS:
        raise AnsibleFilterError(
            f"authoritative DNS serial {position} must be an unsigned 32-bit integer"
        )
    return serial


def mysql_role_dns_converged_serial(values, records_need_update=False):
    """Choose one advancing serial for the two authoritative DNS servers."""
    if not isinstance(values, (list, tuple)) or len(values) != 2:
        raise AnsibleFilterError(
            "runtime role DNS requires exactly two authoritative DNS serials"
        )

    first, second = (_serial(value, index) for index, value in enumerate(values, 1))
    if first == second:
        newest = first
        advance = bool(records_need_update)
    else:
        distance = (second - first) % _SERIAL_MODULUS
        if distance == _SERIAL_HALF_RANGE:
            raise AnsibleFilterError(
                "authoritative DNS serial order is ambiguous in RFC 1982 sequence space"
            )
        newest = second if distance < _SERIAL_HALF_RANGE else first
        advance = True

    return (newest + 1) % _SERIAL_MODULUS if advance else newest


class FilterModule:
    def filters(self):
        return {
            "mysql_role_dns_converged_serial": mysql_role_dns_converged_serial,
        }
