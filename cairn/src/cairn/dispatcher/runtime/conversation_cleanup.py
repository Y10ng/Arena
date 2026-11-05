"""新 DELETE 的精确资源回收；持久请求先绑定身份，宿主材料不随 Docker 对象删除。"""

from __future__ import annotations

from docker.errors import DockerException, NotFound
from requests.exceptions import RequestException


def cleanup_conversation(manager, client, request):
    conversation_id = request["conversation_id"]
    targets = request["targets"]
    bound = targets is not None

    def report(action, error_code=None):
        return client.conversation_cleanup(conversation_id, {
            "request_id": request["request_id"], "action": action,
            "targets": targets if bound else None, "error_code": error_code,
        })

    try:
        # 与主容器创建共用跨进程锁；删除受理前 Server 已确认没有在途轮次。
        with manager._ensure_running_lock(manager.chat_container_name(conversation_id)):
            daemon_id = manager.docker_daemon_id()
            bridge = manager.traffic_execution
            runtime = None
            manifest = manager.chat_directory(conversation_id) / "research" / "manifest.json"
            if manifest.is_file():
                runtime = bridge.conversation_runtime(conversation_id)
            if targets is None:
                common = {"conversation_id": conversation_id, "machine_id": bridge.dispatcher_id,
                          "instance_id": manager.instance_id, "daemon_id": daemon_id}
                targets = []
                control = manager._get_container(manager.chat_container_name(conversation_id))
                if control is not None:
                    manager._assert_owned(control, conversation_id=conversation_id)
                    labels = control.attrs["Config"]["Labels"]
                    targets.append({**common, "kind": "container", "id": control.id, "resource_key": "control",
                                    "labels": {key: labels[key] for key in (
                                        manager._LABEL_INSTANCE_ID, manager._LABEL_ENGINE,
                                        manager._LABEL_SCENARIO_KEY, "cairn.conversation_id")}})
                if runtime is not None:
                    for key, entry in sorted(runtime.manifest["resources"].items()):
                        if entry.get("removed"):
                            continue
                        # 创建后尚未写入 ID 的情况只使用持久随机创建标签查找，不按前缀认领。
                        runtime._resource(entry)
                        if entry["id"] is not None:
                            targets.append({**common, "kind": entry["kind"], "id": entry["id"],
                                            "resource_key": key, "labels": entry["labels"]})
                targets.sort(key=lambda item: (item["kind"], item["resource_key"], item["id"]))
            response = client.conversation_cleanup(conversation_id, {
                "request_id": request["request_id"], "action": "bind", "targets": targets,
            })
            if not response.ok:
                return False
            bound = True
            objects = []
            for target in targets:
                if (target["conversation_id"] != conversation_id or target["instance_id"] != manager.instance_id
                        or target["machine_id"] != bridge.dispatcher_id or target["daemon_id"] != daemon_id):
                    raise RuntimeError("conversation_cleanup_identity_conflict")
                collection = manager._client.containers if target["kind"] == "container" else manager._client.networks
                try:
                    obj = collection.get(target["id"])
                except NotFound:
                    continue
                obj.reload()
                labels = obj.attrs.get("Config", {}).get("Labels", {}) if target["kind"] == "container" else obj.attrs.get("Labels", {})
                if obj.id != target["id"] or any(labels.get(key) != value for key, value in target["labels"].items()):
                    raise RuntimeError("conversation_cleanup_identity_conflict")
                if target["resource_key"] == "control":
                    manager._assert_owned(obj, conversation_id=conversation_id)
                elif (labels.get("io.cairn.traffic.conversation") != conversation_id
                      or labels.get("io.cairn.traffic.dispatcher") != bridge.dispatcher_id
                      or labels.get("io.cairn.traffic.instance") != manager.instance_id):
                    raise RuntimeError("conversation_cleanup_identity_conflict")
                objects.append((target, collection, obj))
            # collector 先沿原 scope 停止和落盘；不删除 capture、workspace、control 或 session。
            if runtime is not None:
                runtime.close()
            for target, collection, obj in objects:
                if manager.docker_daemon_id() != daemon_id:
                    raise RuntimeError("conversation_cleanup_identity_conflict")
                try:
                    if target["kind"] == "container":
                        obj.remove(force=True)
                    else:
                        obj.remove()
                except NotFound:
                    pass
                try:
                    collection.get(target["id"])
                except NotFound:
                    pass
                else:
                    raise RuntimeError("conversation_cleanup_removal_unconfirmed")
                if runtime is not None and target["resource_key"] != "control":
                    runtime.manifest["resources"][target["resource_key"]]["removed"] = True
                    runtime._save()
            if manager.docker_daemon_id() != daemon_id:
                raise RuntimeError("conversation_cleanup_identity_conflict")
            return report("completed").ok
    except (DockerException, RequestException, OSError, RuntimeError, ValueError):
        # 不把 Docker/HTTP 异常原文、标签或路径公开给用户。持久请求在有界预算内续办。
        try:
            report("failed", "conversation_cleanup_unconfirmed")
        except RequestException:
            pass
        return False
