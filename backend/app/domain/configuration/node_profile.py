"""统一规则执行容器的版本与名称。"""

NODE_PROFILE_VERSION = "rule_library_v1"
NODE_DISPLAY_NAME = "规则执行"


def node_summary() -> dict[str, str]:
    return {"profile_version": NODE_PROFILE_VERSION, "display_name": NODE_DISPLAY_NAME}
