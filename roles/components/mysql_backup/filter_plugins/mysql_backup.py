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
    }


def mysql_backup_service_is_idle(stdout, rc):
    state = mysql_systemd_properties(stdout, rc)
    return (
        state["query_ok"]
        and state["load_state"] == "loaded"
        and state["active_state"] == "inactive"
    )


class FilterModule:
    def filters(self):
        return {
            "mysql_systemd_properties": mysql_systemd_properties,
            "mysql_backup_service_is_idle": mysql_backup_service_is_idle,
        }
