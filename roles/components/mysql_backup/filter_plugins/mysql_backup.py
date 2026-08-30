def mysql_systemd_properties(stdout, rc):
    properties = {}
    if isinstance(stdout, str):
        for line in stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator and key:
                properties[key] = value
    return {
        "query_ok": rc == 0,
        "load_state": properties.get("LoadState", ""),
        "active_state": properties.get("ActiveState", ""),
        "sub_state": properties.get("SubState", ""),
        "unit_file_state": properties.get("UnitFileState", ""),
    }


def mysql_backup_service_is_idle(stdout, rc):
    state = mysql_systemd_properties(stdout, rc)
    return (
        state["query_ok"]
        and state["load_state"] == "loaded"
        and state["active_state"] == "inactive"
    )


def mysql_backup_timer_snapshot(stdout, rc):
    state = mysql_systemd_properties(stdout, rc)
    active_state = state["active_state"]
    unit_file_state = state["unit_file_state"]
    state.update(
        {
            "valid": (
                state["query_ok"]
                and state["load_state"] == "loaded"
                and active_state in {"active", "inactive"}
                and unit_file_state in {"enabled", "disabled"}
            ),
            "active": active_state == "active",
            "enabled": unit_file_state == "enabled",
        }
    )
    return state


class FilterModule:
    def filters(self):
        return {
            "mysql_systemd_properties": mysql_systemd_properties,
            "mysql_backup_service_is_idle": mysql_backup_service_is_idle,
            "mysql_backup_timer_snapshot": mysql_backup_timer_snapshot,
        }
