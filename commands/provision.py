"""Resolve declared secrets before starting Ansible with volatile extra variables."""

import argparse
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from contextlib import nullcontext

import yaml

from atlas_core.execution import get_run_directory, temporary_run_directory
from atlas_core.secrets import (
    SecretConfigurationError,
    SecretResolutionError,
    load_provider,
)


class SecretText(str):
    """Ansible values must remain literal, even when they contain Jinja syntax."""


class SecretDumper(yaml.SafeDumper):
    """Encode only string values with Ansible's supported unsafe scalar tag."""


SecretDumper.add_representer(
    SecretText, lambda dumper, value: dumper.represent_scalar("!unsafe", str(value))
)


class DeclarationLoader(yaml.SafeLoader):
    """Reject duplicate variable declarations."""


def construct_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise SecretConfigurationError("duplicate required secret variable")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


DeclarationLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)


def declarations(path):
    try:
        with path.open(encoding="utf-8") as handle:
            # This SafeLoader subclass only rejects duplicate keys.
            raw = yaml.load(handle, Loader=DeclarationLoader)  # noqa: S506
        if not isinstance(raw, dict) or set(raw) != {"required_secrets"}:
            raise ValueError
        mapping = raw["required_secrets"]
        if not isinstance(mapping, dict):
            raise TypeError
        for variable, name in mapping.items():
            if (
                not isinstance(variable, str)
                or not re.fullmatch(r"[a-z][a-z0-9_]*", variable)
                or variable.startswith(("ansible_", "atlas_"))
                or not isinstance(name, str)
            ):
                raise ValueError
        return mapping
    except Exception:
        raise SecretConfigurationError("invalid required secret declaration") from None


def _stop(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # A terminated group leader does not prove its descendants have exited.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run(playbook, required, arguments, *, provider=None, executable=None):
    """Resolve all values, use owner-only volatile storage, and remove it on exit."""
    provider = load_provider() if provider is None else provider
    resolved = provider.get_many(list(required.values()))
    if not isinstance(resolved, dict) or any(
        not isinstance(resolved.get(name), str) or not resolved[name]
        for name in required.values()
    ):
        raise SecretResolutionError("required secrets could not be resolved")
    values = {variable: SecretText(resolved[name]) for variable, name in required.items()}
    try:
        managed = get_run_directory()
    except (ValueError, OSError):
        raise SecretConfigurationError("volatile secret storage is unavailable") from None
    # The supervisor owns managed storage until the entire execution group stops.
    storage = (nullcontext(Path(tempfile.mkdtemp(prefix="provision-", dir=managed)))
               if managed is not None else temporary_run_directory())
    previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}

    spawning = False
    pending_signal = None

    def interrupted(signum, frame):
        nonlocal pending_signal
        if spawning:
            pending_signal = signum
            return
        raise SystemExit(128 + signum)

    for number in previous:
        signal.signal(number, interrupted)
    try:
        with storage as directory:
            path = Path(directory) / "vars.yml"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                yaml.dump(values, handle, Dumper=SecretDumper)
            del values, resolved
            environment = {key: value for key, value in os.environ.items() if not key.startswith("ANSIBLE_")}
            root = Path(__file__).resolve().parents[1]
            environment.update({
                "ANSIBLE_CONFIG": str(root / "ansible.cfg"),
                "ANSIBLE_LOG_PATH": os.devnull,
                "ANSIBLE_LOCAL_TEMP": str(Path(directory) / "ansible"),
                "ANSIBLE_DEBUG": "False",
                "ANSIBLE_CACHE_PLUGIN": "memory",
                "ANSIBLE_RETRY_FILES_ENABLED": "False",
                "ANSIBLE_STDOUT_CALLBACK": "default",
                "ANSIBLE_CALLBACKS_ENABLED": "default",
                "ANSIBLE_LOAD_CALLBACK_PLUGINS": "False",
            })
            # Atlas may resolve the interpreter symlink outside the program venv.
            bin_dir = (Path(os.environ["ATLAS_VENV"]) / "bin"
                       if os.environ.get("ATLAS_VENV") else Path(sys.executable).parent)
            binary = str(bin_dir / "ansible-playbook") if executable is None else executable
            argv = [binary, str(playbook), *arguments, "--extra-vars", f"@{path}"]
            # Ansible can render secrets in parser errors as well as task output.
            # Report only exit status; task output is not a safe diagnostic channel.
            process = None
            try:
                spawning = True
                try:
                    process = subprocess.Popen(argv, cwd=root, env=environment, start_new_session=managed is None,
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                finally:
                    spawning = False
                if pending_signal is not None:
                    raise SystemExit(128 + pending_signal)
                result = process.wait()
            except BaseException:
                for number in previous:
                    signal.signal(number, signal.SIG_IGN)
                if process is not None:
                    if managed is None:
                        _stop(process)
                    else:
                        process.kill()
                        process.wait()
                raise
            if managed is None:
                _stop(process)
            return 128 - result if result < 0 else result
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("playbook", type=Path)
    parser.add_argument("--required-secrets", type=Path, required=True)
    parser.add_argument("--limit", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        arguments = ["--limit", args.limit]
        if args.check:
            arguments.append("--check")
        result = run(args.playbook, declarations(args.required_secrets), arguments)
    except (SecretConfigurationError, SecretResolutionError, OSError, ValueError):
        print("provision: secret configuration, retrieval, or execution failed", file=sys.stderr)
        return 2
    print(f"provision: Ansible exited with status {result}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
