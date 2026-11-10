"""TaskRun 测试网络及采集资源的持久所有权；恢复只停执行、保存材料。"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import shlex
import socket
import threading
import time
import uuid

import docker
from docker.models.containers import Container
import requests

from cairn.server.schemas.traffic import TrafficOwner
from .pause_clock import ExecutionSceneLost, PauseClock
from .traffic_sync import _save_receipt


WORKSPACE = "/home/kali/workspace"
EXECUTION_DIRECTORY = "/run/cairn-exec"
COLLECTOR_UID = 21001
EGRESS_UID = 21002
OWNER_PROTOCOL = "cairn-execution-owner-v1"


@dataclass(frozen=True)
class TrafficScope:
    id: str
    project_id: str
    task_run_id: str
    generation: int
    container_id: str
    address: str
    socket_directory: Path

    def capability(self) -> dict:
        return {
            "protocol": "cairn-task-execution-v1", "scope_id": self.id,
            "task_run_id": self.task_run_id,
            "socket_path": f"{EXECUTION_DIRECTORY}/{self.id}/execute.sock",
        }


class ProjectTrafficRuntime:
    def __init__(
        self, client: docker.DockerClient, *, project_id: str | None, root: Path,
        gateway_image: str, executor_image: str, isolated: bool = False,
        dispatcher_id: str = "isolated-fixture", pause_clock: PauseClock | None = None,
        cancelled=None, conversation_id: str | None = None, workspace: Path | None = None,
        instance_id: str | None = None,
    ) -> None:
        self.client = client
        self.project_id = project_id
        self.conversation_id = conversation_id
        self.traffic_owner = TrafficOwner(project_id=project_id, conversation_id=conversation_id)
        self.root = root.resolve()
        self.gateway_image = gateway_image
        self.executor_image = executor_image
        self.isolated = isolated
        self.dispatcher_id = dispatcher_id
        self.instance_id = instance_id
        self.daemon_id = client.info()["ID"] if conversation_id is not None else None
        if conversation_id is not None and not instance_id:
            raise ValueError("traffic_conversation_instance_required")
        self.workspace = workspace.resolve() if workspace is not None else self.root / "workspace"
        self.control = self.root / "control"
        self.control_prompts = self.root / "control-prompts"
        self.execution = self.root / "execution"
        self.capture = self.root / "capture"
        self.ca = self.capture / "ca"
        self.upstream_ca = self.capture / "upstream-ca.pem"
        self.collector_arguments: list[str] = []
        self._lock = threading.RLock()
        self._entry_lock = threading.RLock()
        self._owners: dict[str, socket.socket] = {}
        self._control_lock = threading.RLock()
        self.pause_clock = pause_clock or PauseClock()
        self._cancelled = cancelled or (lambda: False)
        self.manifest_path = self.root / "manifest.json"
        self.manifest: dict = {}
        self.containers: list[Container] = []
        self.networks = []
        self.scopes: list[TrafficScope] = []
        self.gateway: Container | None = None
        if self.manifest_path.is_file():
            self.manifest = json.loads(self.manifest_path.read_text())
            if (self.manifest.get("project_id") != project_id or self.manifest.get("conversation_id") != conversation_id
                    or self.manifest["dispatcher_id"] != dispatcher_id):
                raise RuntimeError("traffic_resource_owner_mismatch")
            if conversation_id is not None and (self.manifest.get("instance_id") != instance_id
                                                or self.manifest.get("daemon_id") != self.daemon_id):
                raise RuntimeError("traffic_resource_owner_mismatch")
            if type(self.manifest.get("isolated")) is not bool:
                raise RuntimeError("traffic_network_policy_missing")
            self.isolated = self.manifest["isolated"]
        self.owner = self.manifest.get("owner", uuid.uuid4().hex)
        self.labels = {
            "io.cairn.traffic.owner": self.owner,
            **({"io.cairn.traffic.conversation": conversation_id} if conversation_id is not None else {"io.cairn.traffic.project": project_id}),
            "io.cairn.traffic.dispatcher": dispatcher_id,
            **({"io.cairn.traffic.instance": instance_id} if conversation_id is not None else {}),
        }
        self._load_scopes()

    def _save(self) -> None:
        with self._entry_lock:
            _save_receipt(self.manifest_path, self.manifest)

    def _start_container(self, container: Container, scope_id: str | None = None) -> None:
        def cancelled():
            return self._cancelled() or (scope_id is not None and self.manifest["scopes"][scope_id]["entry_closed"])

        def start():
            # 仅启动边界与关闭入口互斥，CA、HTTP、网络与 readiness 等待均在锁外。
            with self._entry_lock:
                if cancelled():
                    return None
                container.start()
                return True

        if self.pause_clock.start(start, cancelled) is None:
            raise RuntimeError("traffic_task_cancelled")

    def _load_scopes(self) -> None:
        self.scopes = [TrafficScope(
            value["id"], self.project_id, value["task_run_id"], value["generation"],
            value["container_id"], value["address"], self.execution / value["id"],
        ) for value in self.manifest.get("scopes", {}).values() if value.get("container_id")]

    def _claim(self, key: str, kind: str, scope_id: str | None = None) -> dict:
        entry = self.manifest["resources"].get(key)
        if entry is None or entry.get("removed"):
            labels = {**self.labels, "io.cairn.traffic.resource": uuid.uuid4().hex,
                      "io.cairn.traffic.role": key.split(":")[0]}
            if scope_id:
                scope = self.manifest["scopes"][scope_id]
                labels.update({
                    "io.cairn.traffic.scope": scope_id,
                    "io.cairn.traffic.task-run": scope["task_run_id"],
                    "io.cairn.traffic.generation": str(scope["generation"]),
                })
            entry = {"kind": kind, "labels": labels, "id": None, "removed": False}
            self.manifest["resources"][key] = entry
            # 创建身份先于 Docker create 落盘，覆盖 create 成功、登记 ID 前的崩溃。
            self._save()
        return entry

    def _resource(self, entry: dict):
        if entry.get("removed"):
            return None
        collection = self.client.containers if entry["kind"] == "container" else self.client.networks
        if entry["id"]:
            try:
                resource = collection.get(entry["id"])
            except docker.errors.NotFound:
                return None
        else:
            matches = collection.list(filters={"label": [f"{key}={value}" for key, value in entry["labels"].items()]},
                                      **({"all": True} if entry["kind"] == "container" else {}))
            if len(matches) > 1:
                raise RuntimeError("traffic_resource_identity_ambiguous")
            if not matches:
                return None
            resource = matches[0]
        resource.reload()
        labels = resource.labels if entry["kind"] == "container" else resource.attrs.get("Labels", {})
        if any(labels.get(key) != value for key, value in entry["labels"].items()):
            raise RuntimeError("traffic_resource_owner_mismatch")
        if entry["id"] is None:
            entry["id"] = resource.id
            self._save()
        return resource

    def _container(self, key: str, image: str, *, scope_id: str | None = None, **kwargs) -> Container:
        if self._cancelled() or (scope_id is not None and self.manifest["scopes"][scope_id]["entry_closed"]):
            raise RuntimeError("traffic_task_cancelled")
        entry = self._claim(key, "container", scope_id)
        container = self._resource(entry)
        if container is None:
            container = self.client.containers.create(image, labels=entry["labels"], **kwargs)
            entry["id"] = container.id
            self._save()
        if all(item.id != container.id for item in self.containers):
            self.containers.append(container)
        return container

    def _network(self, key: str, *, internal: bool):
        entry = self._claim(key, "network")
        network = self._resource(entry)
        if network is None:
            network = self.client.networks.create(
                f"cairn-traffic-{self.owner}-{key}", driver="bridge", internal=internal, labels=entry["labels"],
            )
            entry["id"] = network.id
            self._save()
        if all(item.id != network.id for item in self.networks):
            self.networks.append(network)
        return network

    def _remove(self, key: str) -> None:
        entry = self.manifest["resources"][key]
        resource = self._resource(entry)
        if resource is not None:
            if entry["kind"] == "container":
                if resource.status not in {"exited", "dead", "created"}:
                    raise RuntimeError("traffic_resource_still_running")
                resource.remove()
                self.containers = [item for item in self.containers if item.id != resource.id]
            else:
                resource.remove()
                self.networks = [item for item in self.networks if item.id != resource.id]
        if resource is not None:
            collection = self.client.containers if entry["kind"] == "container" else self.client.networks
            try:
                collection.get(resource.id)
            except docker.errors.NotFound:
                pass
            else:
                raise RuntimeError("traffic_resource_removal_unconfirmed")
        entry["removed"] = True
        self._save()

    def prepare(self, *, upstream_ca: Path | None = None) -> None:
        with self._lock:
            if not self.manifest:
                self.root.mkdir(mode=0o700, parents=True, exist_ok=False)
                self.manifest = {
                    "owner": self.owner, **self.traffic_owner.fields, "dispatcher_id": self.dispatcher_id,
                    **({"instance_id": self.instance_id, "daemon_id": self.daemon_id} if self.conversation_id is not None else {}),
                    "resources": {}, "scopes": {}, "prepared": False, "isolated": self.isolated,
                }
                self._save()
            for directory, uid in ((self.workspace, 1000), (self.control, 1000),
                                   (self.control_prompts, 1000), (self.execution, 1000),
                                   (self.capture, 0), (self.ca, COLLECTOR_UID)):
                directory.mkdir(mode=0o700, exist_ok=True)
                os.chown(directory, uid, uid)
            if upstream_ca is not None:
                if self.upstream_ca.exists() and self.upstream_ca.read_bytes() != upstream_ca.read_bytes():
                    raise RuntimeError("traffic_upstream_ca_conflict")
                if not self.upstream_ca.exists():
                    self.upstream_ca.write_bytes(upstream_ca.read_bytes())
            if self.upstream_ca.exists():
                self.collector_arguments = ["--set", "ssl_verify_upstream_trusted_ca=/run/cairn-traffic/upstream-ca.pem"]
            if not (self.ca / "mitmproxy-ca-cert.pem").exists():
                initializer = self._container(
                    "ca-initializer", self.gateway_image,
                    command=["python", "-c", "from mitmproxy.certs import CertStore; CertStore.from_store('/ca', 'mitmproxy', 2048)"],
                    entrypoint=[], user=f"{COLLECTOR_UID}:{COLLECTOR_UID}", network_mode="none", cap_drop=["ALL"],
                    volumes={str(self.ca): {"bind": "/ca", "mode": "rw"}},
                )
                self._start_container(initializer)
                if initializer.wait(timeout=30)["StatusCode"] != 0:
                    raise RuntimeError("traffic_ca_initialization_failed")
                self._remove("ca-initializer")
            self.internal = self._network("test", internal=True)
            self.egress = self._network("egress", internal=self.isolated)
            self.gateway = self._container(
                "gateway", self.gateway_image, command=["sleep", "infinity"], entrypoint=[],
                detach=True, init=True, network=self.egress.name, cap_drop=["ALL"],
                sysctls={"net.ipv4.ip_forward": "1", "net.ipv4.conf.all.send_redirects": "0"},
            )
            if self.internal.name not in self.gateway.attrs["NetworkSettings"]["Networks"]:
                self.internal.connect(self.gateway)
            if self.gateway.status != "running":
                self._start_container(self.gateway)
            self.gateway.reload()
            self.gateway_address = self.gateway.attrs["NetworkSettings"]["Networks"][self.internal.name]["IPAddress"]
            self.internal.reload()
            subnet = self.internal.attrs["IPAM"]["Config"][0]["Subnet"]
            rule = ["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", subnet, "!", "-d", subnet, "-j", "MASQUERADE"]
            check = ["-C" if value == "-A" else value for value in rule]
            self._initialize_network(self.gateway, ["sh", "-ec", f"{shlex.join(check)} 2>/dev/null || {shlex.join(rule)}"])
            self.manifest["prepared"] = True
            self._save()

    def _initialize_network(self, container: Container, command: list[str]) -> None:
        key = f"network-init:{uuid.uuid4().hex}"
        helper = self._container(
            key, self.gateway_image, command=command, entrypoint=[], user="root",
            network_mode=f"container:{container.id}", cap_drop=["ALL"], cap_add=["NET_ADMIN"],
        )
        scope_id = container.labels.get("io.cairn.traffic.scope")
        self._start_container(helper, scope_id)
        if helper.wait(timeout=20)["StatusCode"] != 0:
            raise RuntimeError("traffic_network_initialization_failed")
        self._remove(key)

    def prepare_scope(
        self, task_run_id: str, generation: int, *, test_egress: dict | None = None,
        executor_image: str | None = None, claim=None, register=None,
    ) -> TrafficScope:
        with self._lock:
            if self._cancelled():
                raise RuntimeError("traffic_task_cancelled")
            if self.gateway is None or type(generation) is not int or generation < 0:
                raise ValueError("traffic_runtime_not_ready")
            if any(value["task_run_id"] == task_run_id for value in self.manifest["scopes"].values()):
                raise RuntimeError("traffic_task_scope_already_exists")
            scope_id = uuid.uuid4().hex
            sockets = self.execution / scope_id
            sockets.mkdir(mode=0o700)
            os.chown(sockets, 1000, 1000)
            self.manifest["scopes"][scope_id] = {
                "id": scope_id, "task_run_id": task_run_id, "generation": generation,
                "container_id": None, "address": None, "entry_closed": False,
                "test_stopped": None, "collector_stopped": None,
            }
            self._save()
            if claim is not None:
                claim(scope_id)
            try:
                return self._start_scope(
                    scope_id, task_run_id, generation, sockets, test_egress or {'mode': 'direct'},
                    executor_image if executor_image is not None else self.executor_image, register,
                )
            except Exception:
                # 准备失败也必须停止已经创建的执行者；原异常不携带目标内容。
                self.stop_scope(scope_id, "traffic_prepare_failed")
                raise

    def _start_scope(
        self, scope_id: str, task_run_id: str, generation: int, sockets: Path,
        test_egress: dict, executor_image: str, register,
    ) -> TrafficScope:
        proxied = test_egress['mode'] != 'direct'
        env = {
            "SSL_CERT_FILE": "/run/cairn-ca/project.pem", "REQUESTS_CA_BUNDLE": "/run/cairn-ca/project.pem",
            "CURL_CA_BUNDLE": "/run/cairn-ca/project.pem", "NODE_EXTRA_CA_CERTS": "/run/cairn-ca/project.pem",
            "PLAYWRIGHT_CLI_SESSION": f"cairn-{scope_id}", "NO_UPDATE_NOTIFIER": "1",
            "CAIRN_EXECUTION_SCOPE": scope_id, "CAIRN_EXECUTION_TASK_RUN": task_run_id,
        }
        if proxied:
            env.update(http_proxy='http://127.0.0.1:8081', https_proxy='http://127.0.0.1:8081',
                       HTTP_PROXY='http://127.0.0.1:8081', HTTPS_PROXY='http://127.0.0.1:8081',
                       NO_PROXY='localhost,127.0.0.1,::1', no_proxy='localhost,127.0.0.1,::1')
        container = self._container(
            f"executor:{scope_id}", executor_image, scope_id=scope_id,
            entrypoint=["python3", "-u", "/opt/cairn/task-executor/server.py"],
            detach=True, init=True, network=self.internal.name, environment=env, working_dir=WORKSPACE,
            volumes={
                str(self.workspace): {"bind": WORKSPACE, "mode": "rw"},
                str(sockets): {"bind": EXECUTION_DIRECTORY, "mode": "rw"},
                str(self.ca / "mitmproxy-ca-cert.pem"): {"bind": "/run/cairn-ca/project.pem", "mode": "ro"},
            },
        )
        state = self.manifest["scopes"][scope_id]
        public_egress = {
            'mode': test_egress['mode'], 'host': test_egress.get('host'), 'port': test_egress.get('port'),
            'revision': test_egress.get('revision', 0),
            'auth_configured': test_egress.get('username') is not None,
        }
        binding = {"id": scope_id, **self.traffic_owner.fields, "task_run_id": task_run_id,
                   "generation": generation, "container_id": container.id, "test_egress": public_egress}
        # durable scope 先于启动；准备尚未就绪时控制器也能核对精确归属。
        if register is not None:
            register(binding)
        state.update(container_id=container.id, test_egress=public_egress)
        self._save()
        self._start_container(container, scope_id)
        container.reload()
        address = container.attrs["NetworkSettings"]["Networks"][self.internal.name]["IPAddress"]
        ipaddress.ip_address(address)
        scope = TrafficScope(scope_id, self.project_id, task_run_id, generation, container.id, address, sockets)
        state.update(address=address, test_stopped=False)
        self._save()
        self.scopes.append(scope)
        spool = self.capture / scope.id
        spool.mkdir(mode=0o700)
        os.chown(spool, COLLECTOR_UID, COLLECTOR_UID)
        binding_path = self.capture / f"{scope.id}.json"
        _save_receipt(binding_path, binding)
        volumes = {
            str(spool): {"bind": "/capture", "mode": "rw"},
            str(self.ca): {"bind": "/run/cairn-traffic/ca", "mode": "ro"},
            str(binding_path): {"bind": "/run/cairn-traffic/scope.json", "mode": "ro"},
        }
        if self.collector_arguments:
            volumes[str(self.upstream_ca)] = {"bind": "/run/cairn-traffic/upstream-ca.pem", "mode": "ro"}
        arguments = list(self.collector_arguments)
        proxy_addresses = []
        if proxied:
            proxy_addresses, egress_volumes = self._start_egress(scope, test_egress)
            volumes.update(egress_volumes)
            arguments += ['--mode', 'dns@127.0.0.1:5353']
        collector = self._container(
            f"collector:{scope.id}", self.gateway_image, scope_id=scope.id,
            command=arguments, detach=True, init=True,
            user=f"{COLLECTOR_UID}:{COLLECTOR_UID}", cap_drop=["ALL"],
            network_mode=f"container:{container.id}", volumes=volumes,
        )
        self._start_container(collector, scope_id)
        state["collector_stopped"] = False
        self._save()
        deadline = time.monotonic() + 30
        while not (spool / "heartbeat").is_file():
            collector.reload()
            if collector.status != "running":
                raise RuntimeError("traffic_collector_start_failed")
            if time.monotonic() >= deadline:
                raise TimeoutError("traffic_collector_start_timeout")
            time.sleep(0.1)
        commands = [
            ["ip", "route", "replace", "default", "via", self.gateway_address],
            ["iptables", "-t", "nat", "-A", "OUTPUT", "-p", "tcp", "-m", "owner", "--uid-owner", str(COLLECTOR_UID), "-j", "RETURN"],
            ["iptables", "-t", "nat", "-A", "OUTPUT", "-d", "127.0.0.1", "-p", "tcp", "-m", "multiport", "--dports", "8081,1080", "-j", "RETURN"],
            ["iptables", "-A", "OUTPUT", "-p", "tcp", "--dport", "8080", "-m", "conntrack", "--ctorigdst", "127.0.0.1", "--ctorigdstport", "8080", "-j", "REJECT"],
            ["iptables", "-A", "OUTPUT", "-p", "tcp", "--dport", "8080", "-m", "conntrack", "--ctorigdst", address, "--ctorigdstport", "8080", "-j", "REJECT"],
            ["iptables", "-A", "INPUT", "!", "-i", "lo", "-p", "tcp", "--dport", "8080", "-j", "REJECT"],
            ["iptables", "-t", "nat", "-A", "OUTPUT", "-p", "tcp", "-j", "REDIRECT", "--to-ports", "8080"],
        ]
        if proxied:
            commands = self._egress_rules(test_egress, proxy_addresses, address)
        self._initialize_network(container, ["sh", "-ec", "\n".join(shlex.join(command) for command in commands)])
        deadline = self.pause_clock.now() + 20
        while not (sockets / "execute.sock").exists() or self.pause_clock.paused:
            # readiness 不持控制锁，也不能把正常冻结误判为启动失败并清理原现场。
            self.pause_clock.wait(lambda: self._cancelled() or state["entry_closed"])
            if self._cancelled() or state["entry_closed"]:
                raise RuntimeError("traffic_task_cancelled")
            container.reload()
            if container.status not in {"running", "paused"} or self.pause_clock.now() >= deadline:
                raise RuntimeError("test_execution_start_failed")
            time.sleep(0.1)
        self._admit_scope(scope.id)
        return scope

    def _start_egress(self, scope: TrafficScope, config: dict) -> tuple[list[str], dict]:
        # 代理地址是稳定配置事实，解析只是定位手段；瞬时 DNS 抖动不应使 scope 准备失败。
        # direct 模式不经此路径；不加缓存层、不加常驻线程。
        for attempt in range(3):
            try:
                addresses = sorted({entry[4][0] for entry in socket.getaddrinfo(
                    config['host'], config['port'], family=socket.AF_INET, type=socket.SOCK_STREAM)})
                break
            except socket.gaierror as exc:
                if exc.errno != socket.EAI_AGAIN or attempt == 2:
                    raise RuntimeError('traffic_proxy_dns_or_address_failed') from None
                time.sleep(2)
        if not addresses or any(ipaddress.ip_address(value).is_loopback for value in addresses):
            raise RuntimeError('traffic_proxy_address_unavailable')
        directory = self.root / 'egress' / scope.id
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(directory, EGRESS_UID, EGRESS_UID)
        names = directory / 'names'
        names.mkdir(mode=0o755)
        os.chown(names, COLLECTOR_UID, COLLECTOR_UID)
        diagnostics = directory / 'diagnostics'
        diagnostics.mkdir(mode=0o755)
        os.chown(diagnostics, EGRESS_UID, EGRESS_UID)
        private = directory / 'private.json'
        _save_receipt(private, {**config, 'host': addresses[0]})
        private.chmod(0o600)
        os.chown(private, EGRESS_UID, EGRESS_UID)
        public = directory / 'public.json'
        _save_receipt(public, {'host': config['host'], 'addresses': addresses})
        public.chmod(0o444)
        bridge = self._container(
            f'egress:{scope.id}', self.gateway_image, scope_id=scope.id,
            entrypoint=['python', '/opt/cairn/traffic/egress.py'], detach=True, init=True,
            user=f'{EGRESS_UID}:{EGRESS_UID}', cap_drop=['ALL'],
            network_mode=f'container:{scope.container_id}',
            volumes={str(directory): {'bind': '/run/cairn-egress', 'mode': 'rw'}},
        )
        self._start_container(bridge, scope.id)
        deadline = time.monotonic() + 15
        while not (directory / 'ready').exists():
            bridge.reload()
            if bridge.status != 'running' or time.monotonic() >= deadline:
                raise RuntimeError('traffic_proxy_transport_start_failed')
            time.sleep(.05)
        return addresses, {str(names): {'bind': '/run/cairn-egress/names', 'mode': 'rw'},
                           str(diagnostics): {'bind': '/run/cairn-egress/diagnostics', 'mode': 'ro'},
                           str(public): {'bind': '/run/cairn-egress/public.json', 'mode': 'ro'}}

    def _egress_rules(self, config: dict, addresses: list[str], address: str) -> list[list[str]]:
        nat = ['iptables', '-t', 'nat', '-A', 'OUTPUT']
        output = ['iptables', '-A', 'OUTPUT']
        collector = ['-m', 'owner', '--uid-owner', str(COLLECTOR_UID)]
        bridge = ['-m', 'owner', '--uid-owner', str(EGRESS_UID)]
        rules = [['ip', 'route', 'replace', 'default', 'via', self.gateway_address]]
        # Docker 的 DNS DNAT 已存在，先截获 DNS，且不让目标名交给宿主解析。
        for protocol in ('udp', 'tcp'):
            rules.append(['iptables', '-t', 'nat', '-I', 'OUTPUT', '1', '-p', protocol,
                          '--dport', '53', '-j', 'REDIRECT', '--to-ports', '5353'])
        # 本地目标先采集，仅 collector 的回环上游保持同 namespace 直连。
        # 托管入口按实际绑定地址和端口排除，不能让业务回环绕过采集。
        rules += [nat + bridge + ['-j', 'RETURN'],
                  nat + ['-p', 'tcp', '-d', '127.0.0.0/8'] + collector + ['-j', 'RETURN'],
                  nat + ['-p', 'tcp', '-d', '127.0.0.1', '-m', 'multiport',
                         '--dports', '8081,1080,9091,5353', '-j', 'RETURN'],
                  nat + ['-p', 'tcp'] + collector + ['-j', 'REDIRECT', '--to-ports', '9091']]
        for host in addresses:
            rules.append(nat + ['-p', 'tcp', '-d', host, '--dport', str(config['port']), '-j',
                                'REDIRECT', '--to-ports', '8081'])
        rules.append(nat + ['-p', 'tcp', '-j', 'REDIRECT', '--to-ports', '8080'])
        # 传输桥唯一外连目的地为上游；即使第三方库选择 DIRECT 也不能绕过。
        for host in addresses:
            rules.append(output + ['-p', 'tcp'] + bridge + ['-d', host, '--dport', str(config['port']), '-j', 'ACCEPT'])
        rules += [output + bridge + ['-m', 'conntrack', '--ctstate', 'ESTABLISHED,RELATED', '-j', 'ACCEPT'],
                  output + bridge + ['-j', 'REJECT'],
                  output + ['-m', 'conntrack', '--ctstate', 'ESTABLISHED,RELATED', '-j', 'ACCEPT'],
                  output + ['-d', '127.0.0.1', '-p', 'tcp', '--dport', '9091'] + collector + ['-j', 'ACCEPT'],
                  output + ['-d', '127.0.0.1', '-p', 'tcp', '--dport', '9091', '-j', 'REJECT'],
                  output + ['-p', 'tcp', '--dport', '8080', '-m', 'conntrack', '--ctorigdst', '127.0.0.1', '--ctorigdstport', '8080', '-j', 'REJECT'],
                  ['iptables', '-A', 'INPUT', '!', '-i', 'lo', '-p', 'tcp', '-m', 'multiport', '--dports', '8080,8081,1080,9091,5353', '-j', 'REJECT'],
                  output + ['-d', '127.0.0.0/8', '-j', 'ACCEPT'],
                  output + ['-j', 'REJECT'],
                  ['ip6tables', '-A', 'OUTPUT', '-o', 'lo', '-j', 'ACCEPT'],
                  ['ip6tables', '-A', 'OUTPUT', '-j', 'REJECT']]
        return rules

    def _admit_scope(self, scope_id: str) -> None:
        # 仅在 collector 和网络规则准备完整后建立一次所有者连接。存续不需要任何
        # 线程续租或持锁 I/O；宿主死亡由内核关闭 fd，显式取消也关闭同一条连接。
        owner = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        state = self.manifest["scopes"][scope_id]
        try:
            with self._entry_lock:
                if state["entry_closed"] or self._cancelled():
                    raise RuntimeError("traffic_task_cancelled")
                if scope_id in self._owners:
                    raise RuntimeError("traffic_owner_already_connected")
                self._owners[scope_id] = owner
            owner.settimeout(20)
            directory_fd = os.open(self.execution / scope_id, os.O_RDONLY | os.O_DIRECTORY)
            try:
                # 正式材料目录含完整实例身份，超过 AF_UNIX 限长；沿用工作区的目录 FD 连接方式。
                owner.connect(f"/proc/self/fd/{directory_fd}/execute.sock")
            finally:
                os.close(directory_fd)
            owner.sendall(("POST /owner HTTP/1.0\r\nX-Cairn-Owner-Protocol: "
                           + OWNER_PROTOCOL + "\r\nContent-Length: 0\r\n\r\n").encode("ascii"))
            owner.settimeout(0.2)
            response = b""
            deadline = self.pause_clock.now() + 20
            while b"\r\n\r\n" not in response:
                # 准备期间的热暂停不计握手预算；等待不持入口锁，取消可以立即断连。
                self.pause_clock.wait(lambda: self._cancelled() or state["entry_closed"])
                if self._cancelled() or state["entry_closed"]:
                    raise RuntimeError("traffic_task_cancelled")
                if self.pause_clock.now() >= deadline:
                    raise RuntimeError("traffic_owner_start_timeout")
                try:
                    chunk = owner.recv(4096)
                except socket.timeout:
                    continue
                if not chunk or len(response) + len(chunk) > 8192:
                    raise RuntimeError("traffic_owner_protocol_mismatch")
                response += chunk
            if (not response.startswith(b"HTTP/1.0 204 ")
                    or f"\r\nX-Cairn-Owner-Protocol: {OWNER_PROTOCOL}\r\n".encode("ascii") not in response):
                raise RuntimeError("traffic_owner_protocol_mismatch")
            owner.settimeout(None)
        except BaseException:
            with self._entry_lock:
                if self._owners.get(scope_id) is owner:
                    self._owners.pop(scope_id)
            owner.close()
            raise

    def check_scope(self, scope_id: str) -> str | None:
        # 心跳年龄与观测耗时不能证明进程死亡。只按 Docker 的真实容器状态处理故障；
        # collector 落盘失败仍由其 durable_hook 退出，网络规则也不会绕过观察点。
        state = self.manifest["scopes"][scope_id]
        if state["entry_closed"]:
            return "traffic_scope_closed"
        roles = ['collector', 'executor']
        if f'egress:{scope_id}' in self.manifest['resources']:
            roles.append('egress')
        for role in roles:
            resource = self._resource(self.manifest["resources"][f"{role}:{scope_id}"])
            expected = {"running", "paused"} if role == "executor" and state.get("hot_paused") else {"running"}
            if resource is None or resource.status not in expected:
                return f"traffic_{role}_stopped"
        return None

    def hot_control(self, paused: bool) -> list[dict[str, str]]:
        """仅冻结测试生产者；采集与宿主上传继续接收在途材料。"""
        resources = []
        if paused:
            self.pause_clock.pause()
        with self._control_lock:
            if not self.manifest:
                return resources
            if paused:
                if not self.manifest.get("control_hot_paused"):
                    self.manifest.update(control_hot_paused=True, control_pause_started=time.monotonic())
                    self._save()
            elif self.manifest.get("control_hot_paused") and self.execution.is_dir():
                total = self.manifest.get("control_paused_seconds", 0.0) + time.monotonic() - self.manifest["control_pause_started"]
                path = self.execution / "paused-seconds"
                temporary = path.with_suffix(".tmp")
                temporary.write_text(str(total))
                temporary.chmod(0o444)
                temporary.replace(path)
                # 解冻失败可重试；只累计本次新窗口，研究域确认恢复后再清暂停标记。
                self.manifest.update(control_pause_started=time.monotonic(), control_paused_seconds=total)
                self._save()
            for scope_id, state in tuple(self.manifest.get("scopes", {}).items()):
                if state.get("termination_confirmed") or not state.get("container_id"):
                    continue
                entry = self.manifest["resources"][f"executor:{scope_id}"]
                container = self._resource(entry)
                if container is not None and container.status == "created" and state.get("address") is None:
                    resources.append({"kind": "test", "container_id": container.id,
                                      "scope_id": scope_id, "task_run_id": state["task_run_id"], "state": "created"})
                    continue
                if container is None or container.status not in {"running", "paused"}:
                    raise ExecutionSceneLost("热暂停现场已丢失，不能原地恢复")
                if paused:
                    if not state.get("hot_paused"):
                        state.update(hot_paused=True, pause_started=time.monotonic())
                        self._save()
                    if container.status == "running":
                        container.pause()
                else:
                    # 所有者连接跨暂停保留；只更新虚拟时钟，避免命令预算计入冻结时间。
                    if state.get("hot_paused"):
                        total = state.get("paused_seconds", 0.0) + time.monotonic() - state["pause_started"]
                        path = self.execution / scope_id / "paused-seconds"
                        temporary = path.with_suffix(".tmp")
                        temporary.write_text(str(total))
                        temporary.chmod(0o444)
                        temporary.replace(path)
                        state.update(paused_seconds=total, pause_started=time.monotonic())
                        self._save()
                    if container.status == "paused":
                        container.unpause()
                    state.update(hot_paused=False)
                    self._save()
                container.reload()
                expected = "paused" if paused else "running"
                if container.status != expected:
                    raise RuntimeError("测试执行域物理状态尚未确认")
                resources.append({"kind": "test", "container_id": container.id,
                                  "scope_id": scope_id, "task_run_id": state["task_run_id"], "state": expected})
        return resources

    def hot_resume_completed(self) -> None:
        with self._control_lock:
            if self.manifest:
                self.manifest["control_hot_paused"] = False
                self._save()

    def _stop_container(self, key: str) -> bool:
        entry = self.manifest["resources"].get(key)
        if entry is None:
            return True
        container = self._resource(entry)
        if container is None:
            return True
        if container.status == "paused":
            container.kill(signal="SIGKILL")
            container.reload()
        elif container.status not in {"exited", "dead", "created"}:
            container.stop(timeout=2)
            container.reload()
        return container.status in {"exited", "dead", "created"}

    def stop_scope(self, scope_id: str, reason: str = "task_finished") -> dict:
        state = self.manifest["scopes"][scope_id]
        with self._entry_lock:
            # 历史采集故障跨重启保留，后续正常关闭原因只更新当前状态。
            failure = state.get("code") if str(state.get("code", "")).startswith("traffic_") else reason
            if str(failure).startswith("traffic_"):
                state.setdefault("failure_code", failure)
            state.update(entry_closed=True, code=reason)
            owner = self._owners.pop(scope_id, None)
            if owner is not None:
                try:
                    owner.shutdown(socket.SHUT_RDWR)
                except OSError:
                    # 对端可能已退出；真正停止仍由下方 Docker 状态核验，不能据此报成功。
                    pass
                finally:
                    owner.close()
        with self._control_lock:
            self._save()
            roles = [('executor', 'test_stopped'), ('collector', 'collector_stopped')]
            if f'egress:{scope_id}' in self.manifest['resources']:
                roles.append(('egress', 'egress_stopped'))
            for role, fact in roles:
                try:
                    if role == "collector" and state["test_stopped"] is True:
                        time.sleep(0.25)
                    state[fact] = self._stop_container(f"{role}:{scope_id}")
                except (docker.errors.DockerException, requests.RequestException, RuntimeError):
                    state[fact] = None
                    state["code"] = "traffic_termination_unknown"
                self._save()
            if state["collector_stopped"] is True:
                self._seal_interrupted(scope_id)
                state.setdefault("sealed_at", time.time())
            confirmed = all(state[fact] is True for _, fact in roles)
            state["termination_confirmed"] = confirmed
            self._save()
            if confirmed:
                # 只有消费进程已确认停止才清除该 scope 的私有认证；原始流量与公开快照保留。
                (self.root / 'egress' / scope_id / 'private.json').unlink(missing_ok=True)
                # 保留停机容器及原挂载；writable layer 也是项目材料。
            return dict(state)

    def _seal_interrupted(self, scope_id: str) -> None:
        entry = self.manifest["resources"].get(f"collector:{scope_id}")
        writer_id = entry["id"] if entry else None
        for path in (self.capture / scope_id / "flows").glob("*/flow.json"):
            record = json.loads(path.read_text())
            if record["state"] != "receiving":
                continue
            last_revision = record["revision"]
            for side in ("request", "response"):
                message = record[side]
                if message is None or message["body_sealed"]:
                    continue
                body = path.parent / f"{side}.body"
                with body.open("rb") as source:
                    os.fsync(source.fileno())
                    actual = os.fstat(source.fileno()).st_size
                if actual < message["body_bytes"]:
                    raise RuntimeError("traffic_durable_body_regressed")
                message.update(body_bytes=actual, body_complete=False, body_sealed=True, body_revision=last_revision + 1)
            record.update(
                revision=last_revision + 1, state="partial" if record["response"] is not None else "failed",
                error={"code": "collector_stopped", "message": None},
                recovery={"source": "runtime", "last_capture_revision": last_revision,
                          "writer_container_id": writer_id, "writer_stopped_at": time.time()},
            )
            # finished_at 保持最后采集事实，不伪造一个不存在的 response/error 事件。
            _save_receipt(path, record)

    def recover(self) -> list[dict]:
        with self._lock:
            # 重启不能冒充原地恢复，也不能为 reconcile 销毁已冻结的现场。
            results = [self.stop_scope(scope_id, "dispatcher_recovered")
                       for scope_id, state in self.manifest.get("scopes", {}).items()
                       if not state.get("hot_paused")]
            # 包括创建后尚未登记 ID 的初始化资源，均只按持久创建身份核对。
            for key, entry in self.manifest.get("resources", {}).items():
                if key.startswith(("network-init:", "ca-initializer")) and not entry.get("removed"):
                    if self._stop_container(key):
                        self._remove(key)
            self._load_scopes()
            return results

    def close(self) -> None:
        with self._control_lock:
            for scope_id in tuple(self.manifest.get("scopes", {})):
                result = self.stop_scope(scope_id, "project_closed")
                if not result["termination_confirmed"]:
                    raise RuntimeError("traffic_termination_unknown")
            for key, entry in list(self.manifest.get("resources", {}).items()):
                if entry["kind"] == "container" and not entry.get("removed"):
                    if not self._stop_container(key):
                        raise RuntimeError("traffic_termination_unknown")
            if self.manifest:
                self.manifest["prepared"] = False
                self._save()

    def destroy(self) -> None:
        """仅项目删除调用；每个 Docker 对象仍须通过原持久身份核验。"""
        self.close()
        with self._lock:
            for kind in ("container", "network"):
                for key, entry in list(self.manifest.get("resources", {}).items()):
                    if entry["kind"] == kind and not entry.get("removed"):
                        self._remove(key)
