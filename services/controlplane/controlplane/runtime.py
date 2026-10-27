"""Where the workloads actually run. The control plane talks to exactly one
runtime:

- KubernetesRuntime: in-cluster API (service-account token). Killing a pod
  means DELETE with gracePeriodSeconds=0; its Deployment/StatefulSet
  controller replaces it.
- DockerRuntime: the Docker Engine API over /var/run/docker.sock (local
  docker-compose). Killing means SIGKILL; the restart policy brings the
  same container back.

Both expose the same small interface so the chaos engine, recovery
tracking and the console are runtime-agnostic.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import httpx

SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


@dataclass
class Pod:
    id: str  # k8s pod name / docker container id
    name: str
    service: str
    status: str  # Running | Pending | Terminating | Restarting | Exited | ...
    ready: bool
    restarts: int
    started_at: datetime | None
    ip: str | None
    node: str | None = None


@dataclass
class ServiceReplicas:
    service: str
    desired: int
    ready: int
    pods: list[Pod] = field(default_factory=list)


class Runtime(Protocol):
    name: str

    async def list_pods(self) -> list[Pod]: ...
    async def desired_replicas(self) -> dict[str, int]: ...
    async def kill(self, pod: Pod) -> None: ...
    async def aclose(self) -> None: ...


def _parse_time(value: str | None) -> datetime | None:
    if not value or value.startswith("0001-01-01"):
        return None
    # docker uses nanoseconds; datetime only takes microseconds
    value = value.replace("Z", "+00:00")
    if "." in value:
        head, rest = value.split(".", 1)
        frac, _, tz = rest.partition("+")
        value = f"{head}.{frac[:6]}+{tz or '00:00'}"
    return datetime.fromisoformat(value)


class KubernetesRuntime:
    name = "kubernetes"

    def __init__(self, namespace: str | None = None, selector: str = "app.kubernetes.io/part-of=pulseops"):
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self.namespace = namespace or (SA_DIR / "namespace").read_text().strip()
        self.selector = selector
        self._token_path = SA_DIR / "token"
        self.client = httpx.AsyncClient(
            base_url=f"https://{host}:{port}", verify=str(SA_DIR / "ca.crt"), timeout=5
        )

    def _headers(self) -> dict:
        # re-read every call: projected tokens are rotated by the kubelet
        return {"Authorization": f"Bearer {self._token_path.read_text().strip()}"}

    @staticmethod
    def _service_of(item: dict) -> str | None:
        return item["metadata"].get("labels", {}).get("app.kubernetes.io/name")

    async def list_pods(self) -> list[Pod]:
        r = await self.client.get(
            f"/api/v1/namespaces/{self.namespace}/pods",
            params={"labelSelector": self.selector}, headers=self._headers(),
        )
        r.raise_for_status()
        pods = []
        for item in r.json()["items"]:
            service = self._service_of(item)
            if not service:
                continue
            status = item.get("status", {})
            conditions = {c["type"]: c["status"] for c in status.get("conditions", [])}
            container_statuses = status.get("containerStatuses", [])
            phase = status.get("phase", "Unknown")
            if item["metadata"].get("deletionTimestamp"):
                phase = "Terminating"
            elif any(cs.get("state", {}).get("waiting", {}).get("reason") == "CrashLoopBackOff" for cs in container_statuses):
                phase = "CrashLoopBackOff"
            pods.append(Pod(
                id=item["metadata"]["name"],
                name=item["metadata"]["name"],
                service=service,
                status=phase,
                ready=conditions.get("Ready") == "True" and phase == "Running",
                restarts=sum(cs.get("restartCount", 0) for cs in container_statuses),
                started_at=_parse_time(status.get("startTime")),
                ip=status.get("podIP"),
                node=item.get("spec", {}).get("nodeName"),
            ))
        return pods

    async def desired_replicas(self) -> dict[str, int]:
        desired: dict[str, int] = {}
        for kind in ("deployments", "statefulsets"):
            r = await self.client.get(
                f"/apis/apps/v1/namespaces/{self.namespace}/{kind}",
                params={"labelSelector": self.selector}, headers=self._headers(),
            )
            r.raise_for_status()
            for item in r.json()["items"]:
                service = self._service_of(item)
                if service:
                    desired[service] = item["spec"].get("replicas", 1)
        return desired

    async def kill(self, pod: Pod) -> None:
        r = await self.client.delete(
            f"/api/v1/namespaces/{self.namespace}/pods/{pod.name}",
            params={"gracePeriodSeconds": 0}, headers=self._headers(),
        )
        r.raise_for_status()

    async def aclose(self) -> None:
        await self.client.aclose()


class DockerRuntime:
    name = "docker"

    def __init__(self, project: str, socket_path: str = "/var/run/docker.sock"):
        self.project = project
        self.client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path), base_url="http://docker", timeout=5
        )

    async def _containers(self) -> list[dict]:
        r = await self.client.get(
            "/containers/json",
            params={"all": "true", "filters": f'{{"label":["com.docker.compose.project={self.project}","pulseops.target=true"]}}'},
        )
        r.raise_for_status()
        return r.json()

    async def list_pods(self) -> list[Pod]:
        pods = []
        for c in await self._containers():
            detail = (await self.client.get(f"/containers/{c['Id']}/json")).json()
            st = detail["State"]
            health = (st.get("Health") or {}).get("Status")
            if st.get("Restarting"):
                status = "Restarting"
            elif st.get("Running"):
                status = "Running"
            else:
                status = st.get("Status", "exited").capitalize()
            networks = detail.get("NetworkSettings", {}).get("Networks", {})
            ip = next((n.get("IPAddress") for n in networks.values() if n.get("IPAddress")), None)
            pods.append(Pod(
                id=c["Id"],
                name=detail["Name"].lstrip("/"),
                service=c["Labels"].get("com.docker.compose.service", "unknown"),
                status=status,
                # no healthcheck defined -> running counts as ready
                ready=status == "Running" and health in (None, "healthy"),
                restarts=detail.get("RestartCount", 0),
                started_at=_parse_time(st.get("StartedAt")),
                ip=ip,
                node="docker",
            ))
        return pods

    async def desired_replicas(self) -> dict[str, int]:
        desired: dict[str, int] = {}
        for c in await self._containers():
            service = c["Labels"].get("com.docker.compose.service", "unknown")
            desired[service] = desired.get(service, 0) + 1
        return desired

    async def kill(self, pod: Pod) -> None:
        """SIGKILL every process in the container from the inside.

        Not `POST /containers/{id}/kill`: Docker records an API kill as a
        *manual* stop and then deliberately does NOT apply the restart
        policy, so nothing would heal. Targets run with `init: true`, so
        PID 1 is a tiny init; `kill -9 -1` takes down the workload, init
        exits, and to Docker that is an unexpected crash — exactly what the
        restart policy exists for.
        """
        r = await self.client.post(
            f"/containers/{pod.id}/exec",
            json={"Cmd": ["sh", "-c", "kill -9 -1"], "AttachStdout": False, "AttachStderr": False},
        )
        if r.status_code == 409:  # container not running (already dead/restarting)
            return
        r.raise_for_status()
        start = await self.client.post(f"/exec/{r.json()['Id']}/start", json={"Detach": True})
        start.raise_for_status()

    async def aclose(self) -> None:
        await self.client.aclose()


def detect_runtime(mode: str, compose_project: str, namespace: str = "") -> Runtime | None:
    mode = mode.lower()
    in_cluster = bool(os.environ.get("KUBERNETES_SERVICE_HOST")) and (SA_DIR / "token").exists()
    if mode == "kubernetes" or (mode == "auto" and in_cluster):
        # namespace of the workloads under test; defaults to our own
        return KubernetesRuntime(namespace or None)
    if mode == "docker" or (mode == "auto" and Path("/var/run/docker.sock").exists()):
        return DockerRuntime(compose_project)
    return None


def now() -> datetime:
    return datetime.now(timezone.utc)
