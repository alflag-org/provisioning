#!/usr/bin/python3
"""Consume one vendor setup credential via stdin; never emit credentials."""

import fcntl
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import sys
from urllib.parse import urlsplit


def messages():
    # Wire fields from gameap/gameap pkg/proto/gateway.proto, EnrollRequest and
    # EnrollResponse. Keep these compatible with the pinned Panel release.
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
    schema = descriptor_pb2.FileDescriptorProto(name='gameap-enroll.proto', package='gameap', syntax='proto3')
    types = descriptor_pb2.FieldDescriptorProto
    specifications = {
        'EnrollRequest': [('setup_key', 1, types.TYPE_STRING), ('host', 2, types.TYPE_STRING),
                          ('port', 3, types.TYPE_INT32), ('os', 4, types.TYPE_STRING),
                          ('version', 5, types.TYPE_STRING), ('capabilities', 6, types.TYPE_STRING)],
        'EnrollResponse': [('success', 1, types.TYPE_BOOL), ('error_message', 2, types.TYPE_STRING),
                           ('node_id', 3, types.TYPE_UINT64), ('api_key', 4, types.TYPE_STRING),
                           ('root_certificate', 5, types.TYPE_STRING), ('server_certificate', 6, types.TYPE_STRING),
                           ('server_private_key', 7, types.TYPE_STRING)],
    }
    for name, fields in specifications.items():
        message = schema.message_type.add(name=name)
        for field, number, kind in fields:
            message.field.add(name=field, number=number, type=kind,
                              label=types.LABEL_REPEATED if field == 'capabilities' else types.LABEL_OPTIONAL)
    pool = descriptor_pool.DescriptorPool()
    pool.Add(schema)
    factory = message_factory.MessageFactory(pool)
    # Debian's distribution protobuf and newer Ubuntu releases expose different APIs.
    build = getattr(message_factory, 'GetMessageClass', None) or factory.GetPrototype
    return tuple(build(pool.FindMessageTypeByName('gameap.' + name)) for name in specifications)


def parse_connect_url(value, endpoint):
    parsed = urlsplit(value)
    if (parsed.scheme != 'grpc' or parsed.netloc != endpoint or parsed.query or parsed.fragment
            or not re.fullmatch(r'/[A-Za-z0-9_-]+', parsed.path)):
        raise ValueError('Unexpected enrollment endpoint or malformed credential')
    return parsed.path[1:]


def require_unenrolled(directory):
    for name in ('gameap-daemon.yaml', 'certs', '.enrollment-attempt'):
        if os.path.lexists(directory / name):
            raise ValueError('Existing or uncertain enrollment requires operator inspection')


def write_exclusive(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def save_identity(directory, response, endpoint):
    if (not response.success or response.node_id <= 0 or not response.api_key
            or '-----BEGIN CERTIFICATE-----' not in response.root_certificate
            or '-----BEGIN CERTIFICATE-----' not in response.server_certificate
            or not re.search(r'-----BEGIN (?:RSA |EC )?PRIVATE KEY-----', response.server_private_key)):
        raise ValueError('Panel did not return a complete enrollment identity')
    certs = directory / 'certs'
    certs.mkdir(mode=0o700)
    for name, content in [('ca.crt', response.root_certificate), ('server.crt', response.server_certificate),
                          ('server.key', response.server_private_key)]:
        write_exclusive(certs / name, content)
    config = {
        'ds_id': response.node_id, 'api_key': response.api_key,
        'ca_certificate_file': str(certs / 'ca.crt'),
        'certificate_chain_file': str(certs / 'server.crt'),
        'private_key_file': str(certs / 'server.key'),
        'grpc': {'address': endpoint}, 'work_path': '/srv/gameap',
        'steamcmd_path': '/srv/gameap/steamcmd', 'if_list': [], 'drives_list': [],
        'log_level': 'info', 'output_log': '/var/log/gameap-daemon/output.log',
        'process_manager': {'name': 'systemd'},
    }
    # JSON is valid YAML and avoids interpolating vendor credentials into YAML scalars.
    write_exclusive(directory / 'gameap-daemon.yaml', json.dumps(config) + '\n')


def enroll(directory, request):
    import grpc
    request_class, response_class = messages()
    with (directory / '.enrollment.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require_unenrolled(directory)
        key = parse_connect_url(request['connect_url'], request['endpoint'])
        ca = request['ca_certificate'].encode()
        if b'-----BEGIN CERTIFICATE-----' not in ca:
            raise ValueError('Missing trusted Panel CA')
        installed = subprocess.run(['/usr/bin/gameap-daemon', 'version'], check=True, capture_output=True,
                                   text=True, timeout=10).stdout
        version = re.search(r'^GameAP Daemon version: (v?[0-9]+\.[0-9]+\.[0-9]+)$', installed, re.MULTILINE)
        if version is None:
            raise ValueError('Cannot determine installed Daemon version')
        message = request_class(setup_key=key, host=request['host'], port=31717,
                                os='linux', version=version[1],
                                capabilities=['grpc', 'file_transfer', 'server_status', 'attach', 'http_proxy'])
        with grpc.secure_channel(request['endpoint'], grpc.ssl_channel_credentials(root_certificates=ca),
                                 options=[('grpc.enable_retries', 0)]) as channel:
            grpc.channel_ready_future(channel).result(timeout=15)
            call = channel.unary_unary('/gameap.DaemonGateway/Enroll',
                                       request_serializer=request_class.SerializeToString,
                                       response_deserializer=response_class.FromString)
            marker = directory / '.enrollment-attempt'
            write_exclusive(marker, 'An enrollment RPC was attempted. Inspect Panel state before retrying.\n')
            # Never automatically retry: failure may mean the Panel committed the enrollment.
            response = call(message, timeout=30)
            save_identity(directory, response, request['endpoint'])
            marker.unlink()


def main():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        enroll(Path('/etc/gameap-daemon'), json.load(sys.stdin))
        print('Enrollment completed')
        return 0
    except Exception:
        # RPC errors, parser errors, and response data can contain credentials.
        print('Enrollment failed; inspect prerequisites or existing enrollment state', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
