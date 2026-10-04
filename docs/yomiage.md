# Deploy and operate yomiage

Provisioning prepares the yomiage release and environment, then runs it through
the shared `application@yomiage.service` instance. The controller needs an
authenticated `gh` command to download the private release. Host assignment and
release integrity inputs belong to [inventory](../inventories/default/group_vars/svc_yomiage.yml).

## Configure the application

Resolve the deployed billing API's HTTPS URL and HMAC key ID through the same
secret provider as the application credentials. The HMAC secret must match that
same key ID. Optional settings belong to the
[role defaults](../roles/services/yomiage/defaults/main.yml).

The Bot requests the `MESSAGE_CONTENT` intent. Enable Message Content Intent on
the application's Bot page in the Discord Developer Portal, following the
[official gateway guidance](https://github.com/discord/discord-api-docs/blob/main/developers/events/gateway.mdx).
Give the Bot access to the test text and voice channels before testing commands.

Store application credentials, the billing API URL, and the HMAC key ID in the
Bitwarden Secrets Manager project selected
by the controller's existing `/etc/atlas/secrets.yml`. Give its machine account
read access to those records. Add an entry under `mappings` for every logical
name in the [secret declaration](../inventories/default/secret-requirements/yomiage.yml):

```yaml
mappings:
  yomiage.discord.token:
    secret_id: <Bitwarden-secret-UUID>
```

Extend the existing mappings rather than replacing them. Keep `provider`,
`config`, and unrelated mappings intact. The controller's configured credential
file contains the Bitwarden machine-account access token; it is separate from
the Discord token and must remain caller-owned with mode `0600`. Neither the
provider token nor resolved application inputs belong in inventory, Git,
command arguments, or chat.

`atlas secret check` verifies retrieval without displaying values. Missing
mappings, inaccessible records, and empty values stop Provisioning before
Ansible starts.

The [environment template](../roles/services/yomiage/templates/environment.j2)
maps the resolved inputs to yomiage's environment variables. Provisioning writes
a root-readable environment file under `no_log` without diff output. systemd
reads it before switching to the application user; that user does not need
permission to read the file. This is a persistent credential copy on the managed
host. The controller's injection files are instead temporary and removed after
execution. Do not manually edit the managed environment file or copy a repository
`.env` onto the host.

## Check and apply

Follow the [normal execution workflow](../README.md#run) with an explicit host
limit, [service convergence](../playbooks/components/services.yml), and the
[yomiage secret declaration](../inventories/default/secret-requirements/yomiage.yml).
The yomiage preparation playbook alone does not start a new instance; service
convergence also runs the generic application component.

Inspect `application@yomiage.service` with systemctl and journalctl on the
managed host.

An active process does not by itself establish Discord or TTS acceptance.
Confirm the Bot connects, then use `/join` and `/settings` in a test server,
post a short message, verify audio playback, and use `/leave`. These checks also
exercise the billing/settings API, Google TTS, and Discord voice connection.

## Rotate credentials

Update the relevant Bitwarden record while retaining its mapping, check retrieval,
and repeat check/apply. A changed environment restarts an existing active instance.
For HMAC rotation, coordinate the API's key ID and secret with the inventory and
provider record so both endpoints agree. Provisioning retains mutable application
state under `/var/lib/yomiage`; removing assignment does not delete it.
