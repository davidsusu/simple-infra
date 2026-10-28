# simple-infra

Minimalistic deployment framework based on Ansible.
Each application supplies **two files**: `infra/app.yml` describes the server and application; `infra/install.yml` contains its own Ansible installation tasks.
No application-specific inventory, role tree, Ansible configuration, or playbook is needed.

## Manual deployment

Use an existing **non-root SSH account with password-protected sudo**.
The framework never creates or edits this account, its password, SSH keys or sudoers.
SSH uses your key/agent.
Every real deployment asks for the sudo password using Ansible's `--ask-become-pass`.
No password is stored in the config or passed in command arguments.
An encrypted SSH key should be loaded into `ssh-agent` first.

Controller: Python 3.11+, an SSH client and dependencies from `requirements.txt`.
Target: Debian 12/13 or Ubuntu 22.04/24.04, amd64/arm64, systemd, Python 3 and sudo.
The account must be able to perform the system administration tasks via sudo; NOPASSWD is neither required nor installed.

```bash
# From the application checkout, with simple-infra next to it:
python3 -m venv .venv
.venv/bin/pip install -r ../simple-infra/requirements.txt
# With uv: uv venv .venv; uv pip install --python .venv/bin/python -r ../simple-infra/requirements.txt

.venv/bin/python ../simple-infra/deploy.py infra/app.yml --validate
.venv/bin/python ../simple-infra/deploy.py infra/app.yml --syntax-check
.venv/bin/python ../simple-infra/deploy.py infra/app.yml
```

The first two commands are local-only and do not prompt for passwords.
Verify the host's SSH fingerprint through a trusted channel and connect once with SSH before deploying: host-key verification remains mandatory.
Paths inside `app.yml` are relative to that file, except `~` in `server.ssh_key`, which uses your local home.
The launcher works regardless of the shell's current directory and uses this framework's Ansible settings.
It creates and deletes a temporary inventory itself.

## Application configuration

```yaml
server:
  host: 203.0.113.10
  user: deploy
  # port: 22
  # ssh_key: ~/.ssh/id_ed25519  # omit to use the SSH agent/default identities

application:
  name: example
  source: ../src

requires: [caddy]
http:
  hosts:
    example.org: 18080
    www.example.org: 18080
    api.example.org: 18081

environment:
  MESSAGE: "Hello!"
```

A name is a 1–26 character lowercase slug (`a-z`, digits, hyphens; starts with a letter).
It determines `/srv/apps/<name>`, the system user/group `app-<name>`, the default service name and the Caddy site filename.
Names are unique per server.
There are no user passwords: service accounts have locked passwords and nologin shells.
They are distinct from both the SSH account and Caddy's `caddy` account.
Caddy retains its official package/service identity and certificate storage.

`source` is a local directory of deployable files, not necessarily Python code.
The launcher makes a deterministic archive, excluding `.git`, `.venv`, Python caches, build/dist and egg-info directories.
Symlinks/special files are rejected.
Do not point it at a directory containing secrets that should not be deployed.
Remote source is extracted under `releases/<digest>` and `src` points to the selected release.
Removed source files cannot linger in the selected release.
Older releases are retained; automated cleanup is not implemented yet.

The baseline installs bash, vim, git, nano, curl, Python and CA certificates.
Optional `requires: [docker]` adds Docker Engine/Buildx/Compose from Docker's stable repository.
`requires: [caddy]` and a nonempty `http.hosts` map must be specified together.
Non-HTTP apps can omit both.
Each map key is a hostname and its value is an integer loopback backend port (1024–65535).
Multiple hosts may share a port within the same application instance.
No wildcard DNS names are accepted in this first version.
Unknown configuration keys fail early.

Environment values must be YAML strings (quote numbers/booleans).
Unicode, quotes, dollar signs, literal `{{ ... }}` and multiline strings are preserved.
The generated files use systemd EnvironmentFile quoting, not shell expansion.
They are root-owned, mode 0640, readable by the application's group.
Do not `source` them in a shell.
Vault/CI secret integration is not part of this manual version.

## Application task contract

`install.yml` is an ordinary Ansible task list, running on the target via sudo.
It receives `app_name`, `app_user`, `app_directory`, `app_environment`, `app_config_directory` (controller-side), `app_http_hosts` (the host-to-port map), `app_domains` (its keys) and `app_http_ports` (sorted unique ports).
The account, source and application directory have already been prepared.

An app can define native services, containers, build steps or other arrangements.
For a common systemd service it can reuse the framework's `service` role:

```yaml
- name: Start the application's own executable
  ansible.builtin.include_role:
    name: service
  vars:
    service_command: /usr/bin/python3 -m your_package
    service_environment:
      PYTHONPATH: "{{ app_directory }}/src"
      PYTHONDONTWRITEBYTECODE: '1'
```

The role renders env/unit files under the app's `infra` directory, registers the unit and restarts only when its source/env/unit changes.
`service_environment` overrides configuration values intentionally, e.g. to bind only to loopback.
`service_suffix` can distinguish multiple services owned by one application.
`service_command` is a systemd ExecStart command, **not a shell script**.
The helper's service filesystem is read-only except private temporary storage; apps needing a different service policy can install their own unit from `install.yml`.
They must manage any writable data directories themselves.

Changes made by custom tasks are the application's responsibility.
In particular, if they generate runtime inputs outside source/env/unit files, the application must arrange any additional restart it needs.

## HTTPS and shared configuration

Every explicit domain/subdomain in `http.hosts` is served through the common Caddy instance, with automatic certificates, renewal and HTTP-to-HTTPS redirects.
Each host routes to its own configured `127.0.0.1:<port>` backend.
The application starts its services and binds them to loopback; the map does not start listeners.
Point every A/AAAA record at the server; do not publish unreachable IPv6 records.
Allow incoming TCP 80/443 in the provider and host firewalls.
DNS, server creation, firewalls and SSH policy are not modified by this framework.

Caddy's persistent `/var/lib/caddy` data is retained.
Each app owns `/srv/apps/<name>/infra/site.caddy`; the shared site's directory holds a symlink.
The combined candidate is validated before replacing the live site.
If commit or reload fails, the previous app site is restored.
A pre-existing root Caddyfile not marked as framework-managed is refused, rather than overwritten.

The final deployment checks every domain's **public HTTPS with certificate validation** and HTTP redirect from the controller.
The sample expects a 200 response at `/`.
DNS/ACME problems can therefore fail the final check even when the local application is running.
Review the failing task and Caddy's journal; do not interpret this as an automatic rollback of the whole application.

## Ownership, updates and limitations

The framework records each application's domains and ports in `/var/lib/simple-infra/apps/<name>.json`.
A different application cannot claim them.
A newly requested port must also be free of unregistered listeners.
These reservations are kept after failures for safe retries; removal/migration is an explicit operation.
All apps on a host must be managed consistently for these checks to be useful.

**Run deployments to one server sequentially.** This first manual version has no cross-process deployment lock.
Account, package, source and service changes are not a global transaction.
Only the Caddy site update has a rollback path.
A renamed application is a new instance, not a rename operation.
Renaming/removing apps, releasing old domain reservations and cleaning stored source releases are not automated.
Application directories are not a security sandbox; install tasks are trusted code with sudo privileges.

Packages use `state: present`: install the current package initially, without implicitly upgrading everything on every deploy.
Existing conflicting Docker packages and unrelated service accounts need explicit migration.
A new config is not a mechanism for changing the SSH account's password.
