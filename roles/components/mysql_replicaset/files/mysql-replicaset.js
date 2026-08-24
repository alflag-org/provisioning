/* global dba, mysql, os, print, shell */

shell.options.useWizards = false;

const action = os.getenv("MYSQL_REPLICASET_ACTION") || "status";
const replicaSetName = os.getenv("MYSQL_REPLICASET_NAME");
const instances = JSON.parse(os.getenv("MYSQL_REPLICASET_INSTANCES") || "[]");
const initialPrimary = os.getenv("MYSQL_REPLICASET_INITIAL_PRIMARY");
const adminUser = os.getenv("MYSQL_REPLICASET_ADMIN_USER");
const adminPassword = os.getenv("MYSQL_REPLICASET_ADMIN_PASSWORD");
const allowedHost = os.getenv("MYSQL_REPLICASET_ALLOWED_HOST");
const target = os.getenv("MYSQL_REPLICASET_TARGET") || "";
const operationTimeout = Number(os.getenv("MYSQL_REPLICASET_TIMEOUT") || "120");
const dryRunOnly = os.getenv("MYSQL_REPLICASET_DRY_RUN_ONLY") === "true";

function connectionOptions(instance) {
  return {
    scheme: "mysql",
    user: adminUser,
    password: adminPassword,
    host: instance.host,
    port: instance.port,
    "ssl-mode": "REQUIRED",
  };
}

function connectTo(instance) {
  shell.connect(connectionOptions(instance));
}

function expectedInstance(name) {
  const match = instances.find((instance) => instance.name === name);
  if (!match) {
    throw new Error(`ReplicaSet target ${name} is not an expected member`);
  }
  return match;
}

function connectToReachableMember() {
  let lastError;
  for (const instance of instances) {
    try {
      connectTo(instance);
      return instance;
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error("No ReplicaSet member is reachable");
}

function instanceNameForMember(endpoint, member) {
  const address = String(member.address || endpoint).split(":")[0].toLowerCase();
  const label = String(member.label || "").toLowerCase();
  const match = instances.find((instance) => {
    return (
      instance.name.toLowerCase() === label ||
      instance.host.toLowerCase() === address ||
      instance.name.toLowerCase() === address
    );
  });
  return match ? match.name : String(member.label || endpoint);
}

function normalizeStatus(status) {
  const topology = status.replicaSet.topology || {};
  const members = Object.keys(topology).map((endpoint) => {
    const member = topology[endpoint];
    return {
      name: instanceNameForMember(endpoint, member),
      address: member.address || endpoint,
      role: member.instanceRole || "UNKNOWN",
      status: member.status || "UNKNOWN",
      mode: member.mode || null,
      replicationLag: member.replicationLag || null,
      instanceErrors: member.instanceErrors || [],
    };
  });
  const primaryMember = members.find(
    (member) => member.role === "PRIMARY" && member.status === "ONLINE"
  );
  const secondaryMember = members.find(
    (member) => member.role === "SECONDARY" && member.status === "ONLINE"
  );
  return {
    status,
    members,
    primary: primaryMember ? primaryMember.name : null,
    secondary: secondaryMember ? secondaryMember.name : null,
  };
}

function readOnlyState(instance) {
  let session;
  try {
    session = mysql.getSession(connectionOptions(instance));
    const row = session
      .runSql(
        "SELECT @@GLOBAL.read_only, @@GLOBAL.super_read_only, " +
          "@@GLOBAL.gtid_executed, @@GLOBAL.binlog_format, @@GLOBAL.gtid_mode, " +
          "@@GLOBAL.enforce_gtid_consistency, @@GLOBAL.log_replica_updates, " +
          "@@GLOBAL.log_bin, @@GLOBAL.server_id, @@GLOBAL.require_secure_transport"
      )
      .fetchOne();
    return {
      reachable: true,
      readOnly: Number(row[0]),
      superReadOnly: Number(row[1]),
      gtidExecuted: String(row[2]),
      binlogFormat: String(row[3]),
      gtidMode: String(row[4]),
      enforceGtidConsistency: String(row[5]),
      logReplicaUpdates: Number(row[6]),
      logBin: Number(row[7]),
      serverId: Number(row[8]),
      requireSecureTransport: Number(row[9]),
    };
  } catch (error) {
    return {reachable: false, error: String(error.message || error)};
  } finally {
    if (session) {
      session.close();
    }
  }
}

function validateServerConfiguration(instance, state) {
  if (!state.reachable) {
    throw new Error(`${instance.name} is unreachable`);
  }
  if (
    state.gtidMode !== "ON" ||
    state.enforceGtidConsistency !== "ON" ||
    state.binlogFormat !== "ROW" ||
    state.logReplicaUpdates !== 1 ||
    state.logBin !== 1 ||
    state.serverId !== instance.serverId ||
    state.requireSecureTransport !== 1
  ) {
    throw new Error(`${instance.name} does not satisfy the ReplicaSet server policy`);
  }
}

function validateExpectedConfigurations() {
  for (const instance of instances) {
    validateServerConfiguration(instance, readOnlyState(instance));
  }
}

function stateWithServerVariables(rs) {
  const normalized = normalizeStatus(rs.status({extended: 1}));
  normalized.serverVariables = {};
  for (const instance of instances) {
    normalized.serverVariables[instance.name] = readOnlyState(instance);
  }
  return normalized;
}

function validateOnlinePair(normalized) {
  if (normalized.members.length !== instances.length) {
    throw new Error("ReplicaSet does not contain every expected member");
  }
  for (const instance of instances) {
    const matches = normalized.members.filter((member) => member.name === instance.name);
    if (matches.length !== 1) {
      throw new Error(`ReplicaSet membership does not match expected node ${instance.name}`);
    }
  }
  const online = normalized.members.filter((member) => member.status === "ONLINE");
  const primaries = online.filter((member) => member.role === "PRIMARY");
  const secondaries = online.filter((member) => member.role === "SECONDARY");
  if (online.length !== 2 || primaries.length !== 1 || secondaries.length !== 1) {
    throw new Error("ReplicaSet must have one online PRIMARY and one online SECONDARY");
  }
}

function validateWritableTopology(normalized) {
  validateOnlinePair(normalized);
  const primary = normalized.serverVariables[normalized.primary];
  if (!primary.reachable || primary.readOnly !== 0 || primary.superReadOnly !== 0) {
    throw new Error("ReplicaSet PRIMARY is not writable");
  }
  for (const instance of instances) {
    if (instance.name === normalized.primary) {
      continue;
    }
    const secondary = normalized.serverVariables[instance.name];
    if (!secondary.reachable || secondary.readOnly !== 1 || secondary.superReadOnly !== 1) {
      throw new Error("ReplicaSet SECONDARY is not read-only");
    }
  }
}

function validatePostTransition(normalized, expectedPrimary) {
  validateWritableTopology(normalized);
  if (normalized.primary !== expectedPrimary) {
    throw new Error(`Expected ${expectedPrimary} to be PRIMARY after the operation`);
  }
}

function metadataSchemaExists(instance) {
  let session;
  try {
    session = mysql.getSession(connectionOptions(instance));
    const row = session
      .runSql(
        "SELECT COUNT(*) FROM information_schema.schemata " +
          "WHERE schema_name = 'mysql_innodb_cluster_metadata'"
      )
      .fetchOne();
    return Number(row[0]) === 1;
  } finally {
    if (session) {
      session.close();
    }
  }
}

function getReplicaSetIfPresent(instance) {
  if (!metadataSchemaExists(instance)) {
    return null;
  }
  return dba.getReplicaSet();
}

let changed = false;
let rs;
let before = null;

if (action === "check") {
  const connected = connectToReachableMember();
  validateExpectedConfigurations();
  rs = getReplicaSetIfPresent(connected);
  if (!rs) {
    print(
      `PROVISIONING_RESULT=${JSON.stringify({
        action,
        changed: false,
        exists: false,
        status: null,
        members: [],
        primary: null,
        secondary: null,
      })}`
    );
  } else {
    const checked = stateWithServerVariables(rs);
    print(
      `PROVISIONING_RESULT=${JSON.stringify({
        action,
        changed: false,
        exists: true,
        status: checked.status,
        members: checked.members,
        primary: checked.primary,
        secondary: checked.secondary,
        serverVariables: checked.serverVariables,
      })}`
    );
  }
} else {
  if (action === "converge") {
    const seed = expectedInstance(initialPrimary);
    connectTo(seed);
    validateExpectedConfigurations();
    rs = getReplicaSetIfPresent(seed);
    if (!rs) {
      for (const instance of instances) {
        if (instance.name !== seed.name && metadataSchemaExists(instance)) {
          throw new Error(
            `${instance.name} has ReplicaSet metadata while the bootstrap seed does not`
          );
        }
      }
      rs = dba.createReplicaSet(replicaSetName, {
        instanceLabel: initialPrimary,
        replicationAllowedHost: allowedHost,
        replicationSslMode: "REQUIRED",
      });
      changed = true;
    }

    let normalized = normalizeStatus(rs.status({extended: 1}));
    for (const instance of instances) {
      const member = normalized.members.find((candidate) => candidate.name === instance.name);
      if (!member) {
        rs.addInstance(connectionOptions(instance), {
          label: instance.name,
          recoveryMethod: "clone",
        });
        changed = true;
      } else if (member.status === "OFFLINE") {
        rs.rejoinInstance(connectionOptions(instance), {
          recoveryMethod: "incremental",
        });
        changed = true;
      } else if (member.status !== "ONLINE") {
        throw new Error(
          `${instance.name} is ${member.status}; automatic rejoin is not unambiguous`
        );
      }
      normalized = normalizeStatus(rs.status({extended: 1}));
    }
  } else {
    connectToReachableMember();
    if (action !== "failover") {
      validateExpectedConfigurations();
    }
    rs = dba.getReplicaSet();
    before = stateWithServerVariables(rs);
    const targetInstance = expectedInstance(target);

    if (action === "switchover") {
      validateOnlinePair(before);
      if (before.secondary !== target) {
        throw new Error("The planned switchover target must be the current online SECONDARY");
      }
      rs.setPrimaryInstance(connectionOptions(targetInstance), {
        dryRun: true,
        timeout: operationTimeout,
      });
      if (!dryRunOnly) {
        rs.setPrimaryInstance(connectionOptions(targetInstance), {
          timeout: operationTimeout,
        });
        changed = true;
      }
    } else if (action === "failover") {
      if (before.primary === target) {
        throw new Error("The emergency failover target is already PRIMARY");
      }
      if (before.primary) {
        throw new Error(
          `Forced failover is refused while ${before.primary} is still an online PRIMARY`
        );
      }
      const candidate = before.members.find((member) => member.name === target);
      if (!candidate || candidate.status !== "ONLINE" || candidate.role !== "SECONDARY") {
        throw new Error("The emergency failover target must be the current online SECONDARY");
      }
      validateServerConfiguration(targetInstance, before.serverVariables[target]);
      rs.forcePrimaryInstance(connectionOptions(targetInstance), {
        dryRun: true,
        invalidateErrorInstances: true,
        timeout: operationTimeout,
      });
      if (!dryRunOnly) {
        rs.forcePrimaryInstance(connectionOptions(targetInstance), {
          invalidateErrorInstances: true,
          timeout: operationTimeout,
        });
        changed = true;
      }
    } else if (action !== "status") {
      throw new Error(`Unsupported ReplicaSet action ${action}`);
    }
  }

  const current = dryRunOnly && before ? before : stateWithServerVariables(rs);
  if (action === "converge" || action === "status") {
    validateWritableTopology(current);
  } else if (action === "switchover" && !dryRunOnly) {
    validatePostTransition(current, target);
  } else if (action === "failover" && !dryRunOnly) {
    if (current.primary !== target) {
      throw new Error(`Expected ${target} to be PRIMARY after forced failover`);
    }
    const newPrimary = current.serverVariables[target];
    validateServerConfiguration(targetInstance, newPrimary);
    if (!newPrimary.reachable || newPrimary.readOnly !== 0 || newPrimary.superReadOnly !== 0) {
      throw new Error("Forced PRIMARY is not writable");
    }
  }

  print(
    `PROVISIONING_RESULT=${JSON.stringify({
      action,
      changed,
      dryRunOnly,
      exists: true,
      before,
      status: current.status,
      members: current.members,
      primary: current.primary,
      secondary: current.secondary,
      serverVariables: current.serverVariables,
    })}`
  );
}
