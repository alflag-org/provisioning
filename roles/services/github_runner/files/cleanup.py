#!/usr/bin/python3
"""Clean only dedicated job state; refuse symlink roots and untrusted PRs."""

import json
import os
from pathlib import Path
import shutil
import stat
import sys


def validate_event(event, repository):
    source = event.get('repository', {})
    if source.get('private') is not True or source.get('full_name') != repository:
        raise ValueError('repository is not permitted')
    if 'pull_request' in event:
        head = event['pull_request'].get('head', {}).get('repo') or {}
        if head.get('full_name') != repository:
            raise ValueError('fork PRs are not permitted')


def clean_directory(path, keep=()):
    # fd-relative rmtree protects against symlink swaps within a job-owned tree.
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimeError('safe deletion is unavailable')
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in os.listdir(descriptor):
            if name in keep:
                continue
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(name, dir_fd=descriptor)
            else:
                os.unlink(name, dir_fd=descriptor)
    finally:
        os.close(descriptor)


def cleanup(work, home, phase):
    if phase not in ('start', 'end'):
        raise ValueError('invalid cleanup phase')
    # The current event and hook script live in _temp; actions/tool setup may have
    # occurred before the start hook. Keep these until the end hook has finished.
    keep = ('_temp', '_actions', '_tool', '_update') if phase == 'start' else ('_update',)
    clean_directory(work, keep)
    clean_directory(home)
    if phase == 'start' and shutil.disk_usage(work).free < 5 * 1024**3:
        raise RuntimeError('less than 5 GiB is available for the job')


def main():
    try:
        phase, repository = sys.argv[1:]
        if phase == 'start':
            event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
            validate_event(event, repository)
        cleanup('/var/lib/github-actions-runner/work', '/var/lib/github-actions-runner/home', phase)
    except Exception:
        # Never expose job-controlled paths, file contents, or event payloads.
        print('Runner job cleanup or repository validation failed', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
