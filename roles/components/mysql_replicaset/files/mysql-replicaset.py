"""Manage an InnoDB ReplicaSet through MySQL Shell's Python AdminAPI."""

import json
import os


def normalize_status(status, instances):
    members = []
    for endpoint, member in status['replicaSet'].get('topology', {}).items():
        address = str(member.get('address') or endpoint).split(':')[0].lower()
        label = str(member.get('label') or '').lower()
        instance = next((item for item in instances if (
            item['name'].lower() == label
            or item['host'].lower() == address
            or item['name'].lower() == address
        )), None)
        members.append({
            'name': instance['name'] if instance else str(member.get('label') or endpoint),
            'address': member.get('address') or endpoint,
            'role': member.get('instanceRole') or 'UNKNOWN',
            'status': member.get('status') or 'UNKNOWN',
            'mode': member.get('mode'),
            'replicationLag': member.get('replicationLag'),
            'instanceErrors': member.get('instanceErrors') or [],
        })
    return {
        'status': status,
        'members': members,
        'primary': next((m['name'] for m in members if m['role'] == 'PRIMARY' and m['status'] == 'ONLINE'), None),
        'secondary': next((m['name'] for m in members if m['role'] == 'SECONDARY' and m['status'] == 'ONLINE'), None),
    }


def validate_server_configuration(instance, state):
    if not state['reachable']:
        raise RuntimeError(f"{instance['name']} is unreachable")
    expected = {'gtidMode': 'ON', 'enforceGtidConsistency': 'ON', 'binlogFormat': 'ROW',
                'logReplicaUpdates': 1, 'logBin': 1, 'serverId': instance['serverId'],
                'requireSecureTransport': 1}
    if any(state[key] != value for key, value in expected.items()):
        raise RuntimeError(f"{instance['name']} does not satisfy the ReplicaSet server policy")


def validate_online_pair(state, instances):
    members = state['members']
    if len(members) != len(instances):
        raise RuntimeError('ReplicaSet does not contain every expected member')
    for instance in instances:
        if sum(m['name'] == instance['name'] for m in members) != 1:
            raise RuntimeError(f"ReplicaSet membership does not match expected node {instance['name']}")
    online = [m for m in members if m['status'] == 'ONLINE']
    if (len(online) != 2 or sum(m['role'] == 'PRIMARY' for m in online) != 1
            or sum(m['role'] == 'SECONDARY' for m in online) != 1):
        raise RuntimeError('ReplicaSet must have one online PRIMARY and one online SECONDARY')


def validate_writable_topology(state, instances):
    validate_online_pair(state, instances)
    for instance in instances:
        variables = state['serverVariables'][instance['name']]
        expected = 0 if instance['name'] == state['primary'] else 1
        if (not variables['reachable'] or variables['readOnly'] != expected
                or variables['superReadOnly'] != expected):
            raise RuntimeError(f"{instance['name']} has incorrect write protection")


def validate_failover_candidate(state, target):
    if state['primary']:
        raise RuntimeError('Forced failover is refused while an online PRIMARY exists')
    candidate = next((m for m in state['members'] if m['name'] == target), None)
    # A replica reconnecting to the unavailable PRIMARY is CONNECTING.
    # AdminAPI dry-run checks its transaction set and applier before promotion.
    if (not candidate or candidate['status'] not in ('ONLINE', 'CONNECTING')
            or candidate['role'] != 'SECONDARY'):
        raise RuntimeError('The emergency failover target must be a reachable SECONDARY')


class ReplicaSetManager:
    def __init__(self, shell, dba, mysql, environment):
        self.shell, self.dba, self.mysql = shell, dba, mysql
        self.instances = json.loads(environment.get('MYSQL_REPLICASET_INSTANCES', '[]'))
        self.name = environment['MYSQL_REPLICASET_NAME']
        self.initial_primary = environment['MYSQL_REPLICASET_INITIAL_PRIMARY']
        self.admin_user = environment['MYSQL_REPLICASET_ADMIN_USER']
        self.admin_password = environment['MYSQL_REPLICASET_ADMIN_PASSWORD']
        self.allowed_host = environment['MYSQL_REPLICASET_ALLOWED_HOST']
        self.target = environment.get('MYSQL_REPLICASET_TARGET', '')
        self.timeout = int(environment.get('MYSQL_REPLICASET_TIMEOUT', '120'))
        self.dry_run = environment.get('MYSQL_REPLICASET_DRY_RUN_ONLY') == 'true'

    def expected_instance(self, name):
        for instance in self.instances:
            if instance['name'] == name:
                return instance
        raise RuntimeError(f'ReplicaSet target {name} is not an expected member')

    def connection_options(self, instance):
        return {'scheme': 'mysql', 'user': self.admin_user, 'password': self.admin_password,
                'host': instance['host'], 'port': instance['port'], 'ssl-mode': 'REQUIRED'}

    def connect_to(self, instance):
        self.shell.connect(self.connection_options(instance))

    def connect_to_reachable_member(self):
        for instance in self.instances:
            try:
                self.connect_to(instance)
                return instance
            except Exception:
                continue
        raise RuntimeError('No ReplicaSet member is reachable')

    def read_only_state(self, instance):
        session = None
        try:
            session = self.mysql.get_session(self.connection_options(instance))
            row = session.run_sql(
                'SELECT @@GLOBAL.read_only, @@GLOBAL.super_read_only, '
                '@@GLOBAL.gtid_executed, @@GLOBAL.binlog_format, @@GLOBAL.gtid_mode, '
                '@@GLOBAL.enforce_gtid_consistency, @@GLOBAL.log_replica_updates, '
                '@@GLOBAL.log_bin, @@GLOBAL.server_id, @@GLOBAL.require_secure_transport'
            ).fetch_one()
            return {'reachable': True, 'readOnly': int(row[0]), 'superReadOnly': int(row[1]),
                    'gtidExecuted': str(row[2]), 'binlogFormat': str(row[3]), 'gtidMode': str(row[4]),
                    'enforceGtidConsistency': str(row[5]), 'logReplicaUpdates': int(row[6]),
                    'logBin': int(row[7]), 'serverId': int(row[8]), 'requireSecureTransport': int(row[9])}
        except Exception:
            return {'reachable': False}
        finally:
            if session is not None:
                session.close()

    def validate_expected_configurations(self):
        for instance in self.instances:
            validate_server_configuration(instance, self.read_only_state(instance))

    def state(self, replicaset):
        state = normalize_status(replicaset.status({'extended': 1}), self.instances)
        state['serverVariables'] = {i['name']: self.read_only_state(i) for i in self.instances}
        return state

    def metadata_schema_exists(self, instance):
        session = self.mysql.get_session(self.connection_options(instance))
        try:
            row = session.run_sql(
                "SELECT COUNT(*) FROM information_schema.schemata "
                "WHERE schema_name = 'mysql_innodb_cluster_metadata'"
            ).fetch_one()
            return int(row[0]) == 1
        finally:
            session.close()

    def get_replicaset_if_present(self, instance):
        return self.dba.get_replica_set() if self.metadata_schema_exists(instance) else None

    def run(self, action):
        self.shell.options.useWizards = False
        changed, before = False, None
        if action == 'check':
            connected = self.connect_to_reachable_member()
            self.validate_expected_configurations()
            rs = self.get_replicaset_if_present(connected)
            if rs is None:
                return {'action': action, 'changed': False, 'exists': False, 'status': None,
                        'members': [], 'primary': None, 'secondary': None}
            return {'action': action, 'changed': False, 'exists': True, **self.state(rs)}
        if action == 'converge':
            seed = self.expected_instance(self.initial_primary)
            self.connect_to(seed)
            self.validate_expected_configurations()
            rs = self.get_replicaset_if_present(seed)
            if rs is None:
                for instance in self.instances:
                    if instance['name'] != seed['name'] and self.metadata_schema_exists(instance):
                        raise RuntimeError(f"{instance['name']} has ReplicaSet metadata while the bootstrap seed does not")
                rs = self.dba.create_replica_set(self.name, {
                    'instanceLabel': self.initial_primary, 'replicationAllowedHost': self.allowed_host,
                    'replicationSslMode': 'REQUIRED',
                })
                changed = True
            state = normalize_status(rs.status({'extended': 1}), self.instances)
            for instance in self.instances:
                member = next((m for m in state['members'] if m['name'] == instance['name']), None)
                address = f"{instance['host']}:{instance['port']}"
                if member is None:
                    rs.add_instance(address, {'label': instance['name'], 'recoveryMethod': 'clone'})
                    changed = True
                elif member['status'] == 'OFFLINE':
                    rs.rejoin_instance(address, {'recoveryMethod': 'incremental'})
                    changed = True
                elif member['status'] != 'ONLINE':
                    raise RuntimeError(f"{instance['name']} is {member['status']}; automatic rejoin is not unambiguous")
                state = normalize_status(rs.status({'extended': 1}), self.instances)
        elif action in ('status', 'switchover', 'failover'):
            self.connect_to_reachable_member()
            if action != 'failover':
                self.validate_expected_configurations()
            rs = self.dba.get_replica_set()
            before = self.state(rs)
            if action in ('switchover', 'failover'):
                target = self.expected_instance(self.target)
                address = f"{target['host']}:{target['port']}"
                options = {'timeout': self.timeout}
                if action == 'switchover':
                    validate_online_pair(before, self.instances)
                    if before['secondary'] != self.target:
                        raise RuntimeError('The planned switchover target must be the current online SECONDARY')
                    transition = rs.set_primary_instance
                else:
                    validate_failover_candidate(before, self.target)
                    validate_server_configuration(target, before['serverVariables'][self.target])
                    transition = rs.force_primary_instance
                    options['invalidateErrorInstances'] = True
                transition(address, {**options, 'dryRun': True})
                if not self.dry_run:
                    transition(address, options)
                    changed = True
        else:
            raise RuntimeError(f'Unsupported ReplicaSet action {action}')

        current = before if self.dry_run and before is not None else self.state(rs)
        if action in ('converge', 'status') or (action == 'switchover' and not self.dry_run):
            validate_writable_topology(current, self.instances)
        if action in ('switchover', 'failover') and not self.dry_run:
            if current['primary'] != self.target:
                raise RuntimeError(f'Expected {self.target} to be PRIMARY after the operation')
            if action == 'failover':
                primary = current['serverVariables'][self.target]
                validate_server_configuration(self.expected_instance(self.target), primary)
                if primary['readOnly'] != 0 or primary['superReadOnly'] != 0:
                    raise RuntimeError('Forced PRIMARY is not writable')
        return {'action': action, 'changed': changed, 'dryRunOnly': self.dry_run,
                'exists': True, 'before': before, **current}


def main():
    from mysqlsh import globals as shell_globals, mysql

    manager = ReplicaSetManager(shell_globals.shell, shell_globals.dba, mysql, os.environ)
    result = manager.run(os.environ.get('MYSQL_REPLICASET_ACTION', 'status'))
    print('PROVISIONING_RESULT=' + json.dumps(result, separators=(',', ':')))


if __name__ == '__main__':
    main()
