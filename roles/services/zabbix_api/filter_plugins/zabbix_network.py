"""Plan ICMP adoption without clearing data or replacing customized measurements."""

import json
import re


OLD = 'ICMP Ping'
NEW = 'Network Generic Device by SNMP'
SUFFIXES = ('Unavailable by ICMP ping', 'High ICMP ping loss', 'High ICMP ping response time')
ICMP_MACROS = {'{$ICMP_LOSS_WARN}', '{$ICMP_RESPONSE_TIME_WARN}'}


def _signature(trigger, items, dependencies):
    try:
        functions = {function['functionid']: json.dumps([
            function['function'], items[function['itemid']], function['parameter']])
            for function in trigger['functions']}
        dependency_names = sorted(dependencies[row['triggerid']] for row in trigger['dependencies'])
    except KeyError:
        raise ValueError('ICMP function or dependency outside the expected entities') from None

    def expression(value):
        return re.sub(r'\{(\d+)\}', lambda match: functions[match[1]], value)

    return {
        'expression': expression(trigger['expression']),
        'recovery_expression': expression(trigger['recovery_expression']),
        'dependencies': dependency_names,
        **{key: trigger[key] for key in ('recovery_mode', 'priority', 'status', 'type',
                                       'manual_close', 'correlation_mode', 'correlation_tag')},
    }


def _item_signature(item):
    fields = ('key_', 'type', 'value_type', 'delay', 'status', 'units',
              'history', 'trends', 'timeout', 'master_itemid')
    try:
        return {**{key: str(item[key]) for key in fields},
                'preprocessing': [{key: step[key] for key in
                                   ('type', 'params', 'error_handler', 'error_handler_params')}
                                  for step in item['preprocessing']]}
    except KeyError:
        raise ValueError('Incomplete ICMP item definition') from None


def unmanaged_icmp_macros(host, declared_macros):
    overrides = {macro['macro'] for macro in host.get('macros', [])} & ICMP_MACROS
    return sorted(overrides - set(declared_macros))


def icmp_migration(host, host_items, host_triggers, template_items, template_triggers, declared_macros):
    """Validate the known transition, including partially completed runs."""
    if not host:
        return {'unlink': False, 'renames': [], 'pending': True}
    host_items = [item for item in host_items if item['hostid'] == host['hostid']]
    host_triggers = [trigger for trigger in host_triggers
                     if any(entry['hostid'] == host['hostid'] for entry in trigger['hosts'])]
    parents = {template['name'] for template in host['parentTemplates']}
    if parents - {OLD, NEW} or {OLD, NEW} <= parents:
        raise ValueError('Unexpected network templates; refusing a destructive replacement')
    if unmanaged_icmp_macros(host, declared_macros):
        raise ValueError('Declare existing ICMP threshold overrides in inventory before adoption')
    if not parents and not host_items and not host_triggers:
        return {'unlink': False, 'renames': [], 'pending': True}
    pairs = []
    for suffix in SUFFIXES:
        old_name, new_name = 'ICMP Ping: ' + suffix, 'Network Generic Device: ' + suffix
        candidates = [trigger for trigger in host_triggers if trigger['description'] in (old_name, new_name)]
        expected = [trigger for trigger in template_triggers if trigger['description'] == new_name]
        if len(candidates) != 1 or len(expected) != 1:
            raise ValueError('Missing or duplicate ICMP triggers; inspect the target before adoption')
        current, desired = candidates[0], expected[0]
        allowed_ids = {'0'} | {trigger['triggerid'] for trigger in template_triggers
                               if trigger['description'] in (old_name, new_name)}
        if current['templateid'] not in allowed_ids:
            raise ValueError('ICMP trigger inherited from an unexpected template')
        pairs.append((suffix, current, desired))
    item_keys = {item['itemid']: item['key_'] for item in host_items}
    template_keys = {item['itemid']: item['key_'] for item in template_items}
    host_dependencies = {current['triggerid']: suffix for suffix, current, _ in pairs}
    template_dependencies = {desired['triggerid']: suffix for suffix, _, desired in pairs}
    desired_item_ids = {function['itemid'] for _, _, desired in pairs for function in desired['functions']}
    old_item_ids = {function['itemid'] for trigger in template_triggers
                    if trigger['description'].startswith('ICMP Ping: ')
                    for function in trigger['functions']}
    for item in host_items:
        old = [entry for entry in template_items if entry['itemid'] in old_item_ids and entry['key_'] == item['key_']]
        new = [entry for entry in template_items if entry['itemid'] in desired_item_ids and entry['key_'] == item['key_']]
        if len(old) != 1 or len(new) != 1 or _item_signature(old[0]) != _item_signature(new[0]):
            raise ValueError('Packaged ICMP item behavior differs; inspect before adoption')
        if item['templateid'] not in ('0', old[0]['itemid'], new[0]['itemid']):
            raise ValueError('ICMP item inherited from an unexpected template')
        if _item_signature(item) != _item_signature(new[0]):
            raise ValueError('Modified ICMP item; refusing to overwrite its behavior')
    renames = []
    for suffix, current, desired in pairs:
        if _signature(current, item_keys, host_dependencies) != _signature(desired, template_keys, template_dependencies):
            raise ValueError('Modified ICMP trigger; refusing to overwrite its behavior')
        if current['description'] != desired['description']:
            renames.append({'triggerid': current['triggerid'], 'description': desired['description']})
    return {'unlink': OLD in parents, 'renames': renames, 'pending': NEW not in parents}


class FilterModule:
    def filters(self):
        return {'zabbix_icmp_migration': icmp_migration,
                'zabbix_unmanaged_icmp_macros': unmanaged_icmp_macros}
