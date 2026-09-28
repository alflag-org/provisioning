"""Run the pinned Ansible CLI with callback discovery restricted to trusted paths."""

from pathlib import Path
import runpy
import sys

import ansible.plugins.callback
from ansible.plugins.loader import PluginPathContext, callback_loader


def callback_paths(subdirs=True):
    # Ansible also discovers callbacks beside playbooks and roles, even when
    # CALLBACK_PLUGINS and CALLBACKS_ENABLED are explicit. Do not import them.
    # This loader hook is verified with the pinned ansible-core integration tests.
    return [
        PluginPathContext(str(Path(__file__).resolve().parents[1] / "callback_plugins"), False),
        PluginPathContext(str(Path(ansible.plugins.callback.__file__).parent), True),
    ]


def main():
    callback_loader._get_paths_with_context = callback_paths
    sys.argv = sys.argv[1:]
    runpy.run_path(sys.argv[0], run_name="__main__")


if __name__ == "__main__":
    main()
