# Internal DNS records

Use [dns.yml](../inventories/default/group_vars/default/dns.yml) to publish an
unmanaged device or service without registering it as an Ansible host. Static
records are applied by the existing authoritative DNS role alongside records
generated from inventory. Editing the file alone does not update DNS servers.

## Managed zones and record sources

The [authoritative DNS inventory](../inventories/default/group_vars/svc_dns_authoritative.yml)
declares forward zones and selects the networks used for reverse zones.

| Forward zone | Record sources |
| --- | --- |
| `srv.alflag.internal` | Host and physical-device identities: inventory records, static records, runtime MySQL CNAMEs |
| `access.internal` | Service and management-interface access names: inventory aliases and static records |

The [recursive DNS inventory](../inventories/default/group_vars/svc_dns_recursive.yml)
routes these two forward zones and managed reverse zones to the authoritative
servers through Unbound stub zones.

Inventory host identities use `network_primary_fqdn` and
`network_ipv4_address` (falling back to `ansible_host`). Service names use
`network_service_aliases`. Only members of the role's record source group are
published. Inventory identities also produce PTR records in matching managed
reverse zones. Static records have no effect on host membership or deployment.

Reverse zones are generated from the configured network CIDRs by
[network_reverse_zones](../filter_plugins/network.py), which expands supported
IPv4 networks into `/24` reverse zones. Their addresses and scope remain defined
in inventory rather than in the static record file.

`mysql-shared-primary` and `mysql-shared-replica` in the runtime MySQL zone are
reserved for CNAMEs selected from live ReplicaSet status. The existing runtime
record include is preserved during ordinary DNS convergence.

## Add static records

Add entries under an existing forward zone in `dns_static_records`. The following
NAS entries are examples only; they are not included in the deployed definition.
Keep unrelated entries already present in the file.

```yaml
dns_static_records:
  srv.alflag.internal:
    - name: storage100
      type: A
      value: 10.10.10.35
      ptr: true
      ttl: 600
  access.internal:
    - name: nas
      type: CNAME
      value: storage100.srv.alflag.internal.
```

`name`, `type`, and `value` are required. Names are relative to the selected zone,
without a trailing dot; dotted relative names are allowed. `@` refers to the zone
apex. Labels use ASCII letters, digits, and internal hyphens, with at most 63
characters per label and 253 characters in the resulting full name. Names are
case insensitive. CNAME targets must be absolute DNS names ending in a dot.
A values must be IPv4 addresses. Only `A` and `CNAME` can be declared directly;
unknown fields and other record types fail validation.

`ttl` is optional and specified as an integer number of seconds from 0 through
2147483647. Omit it to use the zone's existing default TTL. An automatically
generated PTR uses the A record's explicit TTL, or the reverse zone's default
when no TTL is supplied.

`ptr` defaults to `false`. Only an A record can enable it with `ptr: true`.
The example generates `storage100.srv.alflag.internal. IN A 10.10.10.35`
and a PTR at `35.10.10.10.in-addr.arpa.` pointing to
`storage100.srv.alflag.internal.`. The IP must belong to a managed reverse zone;
an address outside that scope fails validation. CNAMEs do not generate PTRs.

## Duplicate and collision constraints

Each full record name may be declared once, including names generated from
inventory. Identical duplicates also fail; records are never silently removed
or overwritten. An A record and a CNAME cannot share a name. A CNAME cannot use
any managed zone's apex because it would coexist with SOA and NS records. Static records
cannot use the runtime MySQL names, including while their include is empty.

Different A names may share an IP if they do not request competing PTRs. Only one
PTR may own an IP's reverse name, including inventory-generated PTRs. Choose a
single A record for `ptr: true` and use CNAMEs or A records without PTRs for its
other service names.

Validation runs at the beginning of the authoritative DNS role, before package
installation, file updates, or service operations. The
[static record filter](../roles/dns_authoritative/filter_plugins/static_records.py)
checks definitions against all configured zones and inventory-generated names.

## Validate and apply DNS

Run local checks after editing; these tests use synthetic data and local
processes and do not connect to DNS servers:

```bash
mise run check
```

For a separately authorized deployment, use the
[normal execution workflow](../README.md#run) with the
[DNS playbook](../playbooks/components/services/dns.yml) and limit it to
`svc_dns_authoritative:svc_dns_recursive` so both authoritative servers receive
the same definition and the recursive servers use the corresponding stub zones.
The DNS role needs no injected secrets; supply a declaration file containing
`required_secrets: {}` to the command's required option:

```bash
atlas run provision playbooks/components/services/dns.yml --limit 'svc_dns_authoritative:svc_dns_recursive' --required-secrets /path/to/empty-secrets.yml --check
atlas run provision playbooks/components/services/dns.yml --limit 'svc_dns_authoritative:svc_dns_recursive' --required-secrets /path/to/empty-secrets.yml
```

Inspect the intended changes before apply. Repeat the same targeted check after
apply, inspect the resulting zone files, and query each authoritative server
directly for the changed forward and reverse names, for example:

```bash
dig @<authoritative-address> storage100.srv.alflag.internal A +norecurse
dig @<authoritative-address> nas.access.internal CNAME +norecurse
dig @<authoritative-address> -x 10.10.10.35 +norecurse
```

Check mode requires the packages, zone directories, and runtime include to be
available for rendering on the managed servers. The provision command reports only Ansible's exit status; a successful
check does not prove convergence or DNS answers. Applying uses `nsd-checkzone`
to validate each managed zone before replacement, retains complete
`nsd-checkconf` and zone checks, and restarts NSD only when managed files change.

The [SOA serial filter](../roles/dns_authoritative/filter_plugins/soa_serial.py)
preserves the current serial when rendered content is unchanged. A content
change advances it once. Reordering static entries does not change rendered
content or request a restart.

## Remove records

Remove the entry from `dns.yml`, validate, and apply the same DNS scope. The next
render removes its forward record and its automatically generated PTR. Changing
`ptr: true` to `false` removes only that PTR. Affected zones advance their serial;
unchanged zones retain theirs. Cached answers can remain until their previous
TTL expires. Removing a static record does not remove any device, service, or
inventory-generated record. Check for CNAMEs still pointing at a removed target;
target existence is not enforced because CNAMEs may refer to external zones.
