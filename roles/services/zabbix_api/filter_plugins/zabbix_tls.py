"""Require peer and hostname verification throughout an exported HTTP template."""


def verify_http_tls(value):
    if isinstance(value, list):
        return [verify_http_tls(child) for child in value]
    if not isinstance(value, dict):
        return value
    result = {key: verify_http_tls(child) for key, child in value.items()}
    if result.get('type') == 'HTTP_AGENT':
        result.update(verify_peer='YES', verify_host='YES')
    return result


class FilterModule:
    def filters(self):
        return {'zabbix_verify_http_tls': verify_http_tls}
