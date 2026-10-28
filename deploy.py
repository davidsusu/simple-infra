#!/usr/bin/env python3
"""Load an application's small config and invoke the shared Ansible playbook."""
import argparse
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

import yaml

ROOT = Path(__file__).resolve().parent
DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def mapping(value, label, keys):
    require(isinstance(value, dict), f"{label} must be a mapping")
    require(set(value) <= set(keys), f"Unknown keys in {label}: {set(value) - set(keys)}")
    return value


def load_config(path):
    path = path.resolve()
    with path.open(encoding="utf-8") as stream:
        cfg = mapping(yaml.safe_load(stream), "config",
                      ["server", "application", "requires", "environment", "http"])
    server = mapping(cfg.get("server"), "server", ["host", "user", "port", "ssh_key"])
    app = mapping(cfg.get("application"), "application", ["name", "source"])
    host, user = server.get("host"), server.get("user")
    require(isinstance(host, str) and re.fullmatch(r"[a-zA-Z0-9_.:-]+", host)
            and not host.startswith('-'), "Invalid SSH hostname/IP")
    require(isinstance(user, str) and re.fullmatch(r"[a-z_][a-z0-9_-]*", user), "Invalid SSH username")
    require(user != "root", "Use a pre-existing non-root SSH user with password-protected sudo")
    ssh_port = server.get("port", 22)
    require(type(ssh_port) is int and 1 <= ssh_port <= 65535, "server.port must be 1..65535")
    name = app.get("name")
    require(isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,25}", name),
            "application.name must be a lowercase slug of 1..26 characters")
    require(user not in ('caddy', f'app-{name}'), "The SSH user must be separate from the service accounts")
    features = cfg.get("requires", [])
    require(isinstance(features, list) and all(f in ["caddy", "docker"] for f in features),
            "Supported features: caddy, docker")
    http = mapping(cfg.get("http", {}), "http", ["hosts"])
    hosts = http.get("hosts", {})
    require(isinstance(hosts, dict), "http.hosts must be a domain-to-port mapping")
    require(all(isinstance(d, str) and len(d) <= 253 and DOMAIN.fullmatch(d) for d in hosts),
            "Use explicit lowercase DNS names, without scheme, path, port or wildcard")
    require(all(type(port) is int and 1024 <= port <= 65535 for port in hosts.values()),
            "Each http.hosts port must be an integer in 1024..65535")
    require(bool(hosts) == ("caddy" in features),
            "HTTP deployment requires nonempty http.hosts and requires: [caddy] together")
    env = mapping(cfg.get("environment", {}), "environment", cfg.get("environment", {}).keys()
                  if isinstance(cfg.get("environment", {}), dict) else [])
    for key, value in env.items():
        require(isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key), "Invalid environment name")
        require(isinstance(value, str), f"Quote environment value {key}; it must be a string")
        require(all(ord(c) != 0 and ord(c) != 0xfeff and not 0xd800 <= ord(c) <= 0xdfff
                    and not 0xfdd0 <= ord(c) <= 0xfdef and ord(c) & 0xffff not in (0xfffe, 0xffff)
                    for c in value), f"Unsupported Unicode in environment value {key}")
    source = app.get("source")
    require(isinstance(source, str) and source, "application.source must name a local source directory")
    source = (path.parent / source).resolve()
    require(source.is_dir(), f"Source directory does not exist: {source}")
    install = path.parent / "install.yml"
    require(install.is_file(), f"Missing application task list: {install}")
    key = server.get("ssh_key")
    if key is not None:
        require(isinstance(key, str) and key, "server.ssh_key must be a path")
        key = Path(key).expanduser()
        key = (path.parent / key).resolve() if not key.is_absolute() else key.resolve()
        require(key.is_file(), f"SSH private key not found: {key}")
    return {
        "app_name": name, "app_directory": f"/srv/apps/{name}", "app_user": f"app-{name}",
        "app_domains": list(hosts), "app_http_hosts": hosts,
        "app_http_ports": sorted(set(hosts.values())),
        "app_features": features, "app_environment": env,
        "app_source_directory": str(source),
        "app_config_directory": str(path.parent), "app_install_tasks": str(install),
        "ssh_host": host, "ssh_user": user, "ssh_port": ssh_port,
        "ssh_key": str(key) if key else None,
    }


def build_source_archive(source, output):
    """Deterministic source bundle; no VCS, caches, environments or symlinks."""
    source = Path(source)
    excluded = {".git", ".venv", "__pycache__", ".pytest_cache", "build", "dist"}
    with tarfile.open(output, "w") as archive:
        for directory, dirs, files in os.walk(source, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in excluded and not d.endswith('.egg-info'))
            for name in dirs + sorted(files):
                path = Path(directory) / name
                if path.suffix in ('.pyc', '.pyo'):
                    continue
                require(not path.is_symlink(), f"Source symlinks are not supported: {path}")
                entry = tarfile.TarInfo(path.relative_to(source).as_posix())
                if path.is_dir():
                    entry.type, entry.mode = tarfile.DIRTYPE, 0o755
                    archive.addfile(entry)
                else:
                    require(path.is_file(), f"Unsupported source entry: {path}")
                    data = path.read_bytes()
                    entry.size = len(data)
                    entry.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                    archive.addfile(entry, io.BytesIO(data))
    return hashlib.sha256(Path(output).read_bytes()).hexdigest()


class Literal(str):
    """Ansible must treat application data as data, including literal {{ text }}."""


class VarsDumper(yaml.SafeDumper):
    pass


VarsDumper.add_representer(Literal, lambda dumper, value:
                          dumper.represent_scalar('!unsafe', str(value)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--validate", action="store_true", help="Validate config and source locally; no SSH or password prompt")
    parser.add_argument("--syntax-check", action="store_true", help="Ansible syntax check without SSH or password prompt")
    args = parser.parse_args(argv)
    try:
        values = load_config(args.config)
        with tempfile.TemporaryDirectory(prefix="simple-infra-") as directory:
            directory = Path(directory)
            archive = directory / 'source.tar'
            values['app_source_digest'] = build_source_archive(values['app_source_directory'], archive)
            values['app_source_archive'] = str(archive)
            if args.validate:
                print(f"Valid: {values['app_name']} -> {values['ssh_user']}@{values['ssh_host']}")
                return 0
            inventory = {'all': {'hosts': {'deployment_target': {
                'ansible_host': values.pop('ssh_host'), 'ansible_user': values.pop('ssh_user'),
                'ansible_port': values.pop('ssh_port'), 'ansible_connection': 'ssh',
                'ansible_ssh_common_args': '-o PasswordAuthentication=no -o KbdInteractiveAuthentication=no -o StrictHostKeyChecking=yes',
            }}}}
            key = values.pop('ssh_key')
            if key:
                inventory['all']['hosts']['deployment_target']['ansible_ssh_private_key_file'] = key
            values['app_environment'] = {k: Literal(v) for k, v in values['app_environment'].items()}
            (directory/'inventory.yml').write_text(yaml.safe_dump(inventory), encoding='utf-8')
            (directory/'vars.yml').write_text(yaml.dump(values, Dumper=VarsDumper, allow_unicode=True), encoding='utf-8')
            ansible = Path(sys.executable).parent/'ansible-playbook'
            executable = str(ansible) if ansible.exists() else shutil.which('ansible-playbook')
            require(executable is not None, "Install simple-infra/requirements.txt in this Python environment")
            command = [executable, '-i', str(directory/'inventory.yml'), str(ROOT/'site.yml'),
                       '-e', '@'+str(directory/'vars.yml')]
            command.append('--syntax-check' if args.syntax_check else '--ask-become-pass')
            env = dict(os.environ, ANSIBLE_CONFIG=str(ROOT/'ansible.cfg'))
            return subprocess.run(command, env=env, cwd=ROOT).returncode
    except (ValueError, OSError, yaml.YAMLError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
