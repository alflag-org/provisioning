"""Limit the packaged Proxmox template before granting API access."""

from copy import deepcopy


DISABLED_ITEMS = {'proxmox_ve.get_user_data'}
DISABLED_RULES = {
    'proxmox_ve.users.discovery',
    'proxmox_ve.node.certificates.discovery[{#NODE.ID}]',
    'proxmox_ve.node.disks.discovery[{#NODE.ID}]',
    'proxmox_ve.node.qemu.filesystem.discovery[{#NODE.ID},{#QEMU.VMID}]',
    'proxmox_ve.node.qemu.network.discovery[{#NODE.ID},{#QEMU.VMID}]',
    'proxmox_ve.node.qemu.os.discovery[{#NODE.ID},{#QEMU.VMID}]',
    'proxmox_ve.node.qemu.network.ip.discovery[{#NODE.ID},{#QEMU.VMID},{#QEMU.NETIF.NAME}]',
}
DISABLED_PROTOTYPES = {
    'proxmox_ve.node.hardware.pci[{#NODE.ID}]',
    'proxmox_ve.node.hardware.usb[{#NODE.ID}]',
    'proxmox_ve.node.time.info[{#NODE.ID}]',
    'proxmox_ve.node.localtime[{#NODE.ID}]',
    'proxmox_ve.node.time[{#NODE.ID}]',
    'proxmox_ve.node.timezone[{#NODE.ID}]',
    'proxmox_ve.node.updates[{#NODE.ID}]',
    'proxmox_ve.node.version[{#NODE.ID}]',
    'proxmox_ve.node.qemu.agent[{#NODE.ID},{#QEMU.VMID}]',
    'proxmox_ve.node.qemu.agent.version[{#NODE.ID},{#QEMU.VMID}]',
    'proxmox_ve.node.qemu.time[{#NODE.ID},{#QEMU.VMID}]',
    'proxmox_ve.node.qemu.timezone[{#NODE.ID},{#QEMU.VMID}]',
}
CORE_COLLECTORS = {
    'proxmox_ve.cluster.resources.get',
    'proxmox_ve.get_node_data',
    'proxmox_ve.node.lxc[{#NODE.ID},{#LXC.VMID}]',
    'proxmox_ve.node.qemu[{#NODE.ID},{#QEMU.VMID}]',
    'proxmox_ve.node.storage.discovery[{#NODE.ID}]',
    'proxmox_ve.node.storage[{#NODE.ID},{#STORAGE.NAME}]',
}


def basic_proxmox_template(value):
    result = deepcopy(value)
    export = result['zabbix_export']
    templates = export['templates']
    if export['version'] != '8.0' or len(templates) != 1 or templates[0]['template'] != 'Proxmox VE by HTTP':
        raise ValueError('Expected the packaged Proxmox VE by HTTP template for Zabbix 8.0')
    template = templates[0]

    def collector(item):
        # A new upstream collector requires review before it receives a token.
        if item.get('type') in {'HTTP_AGENT', 'SCRIPT'} and item['key'] not in CORE_COLLECTORS:
            raise ValueError('Unexpected Proxmox API collector: ' + item['key'])

    for item in template.get('items', []):
        if item['key'] in DISABLED_ITEMS:
            item['status'] = 'DISABLED'
        else:
            collector(item)

    # Zabbix 8 exports nested rules in this flat list, linked by parent_discovery_rule.
    # Stop the rule itself; filtering its response would still call the guest API.
    for rule in template.get('discovery_rules', []):
        if rule['key'] in DISABLED_RULES:
            rule['status'] = 'DISABLED'
            if 'parent_discovery_rule' in rule:
                rule['discover'] = 'NO_DISCOVER'
            continue
        collector(rule)
        for item in rule.get('item_prototypes', []):
            if item['key'] in DISABLED_PROTOTYPES:
                item.update(status='DISABLED', discover='NO_DISCOVER')
                for trigger in item.get('trigger_prototypes', []):
                    trigger.update(status='DISABLED', discover='NO_DISCOVER')
            else:
                collector(item)
    return result


class FilterModule:
    def filters(self):
        return {'zabbix_proxmox_basic_template': basic_proxmox_template}
