import os
import tempfile

# La configurazione viene letta all'import: la cartella dati di test va impostata prima.
_DATA = tempfile.mkdtemp(prefix="jas-test-")
os.environ["JAS_DATA_DIR"] = _DATA
os.environ["JAS_PLUGIN_DIRS"] = ""
os.environ["JAS_OLLAMA_URL"] = "http://127.0.0.1:9"  # nessun Ollama durante i test

import time  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.security import hash_password, login_limiter  # noqa: E402
from app.tasks import JobContext, Param, Task, TaskError, registry  # noqa: E402


class EchoTask(Task):
    """Compito finto e veloce per testare il flusso dei lavori."""

    id = "echo"
    title = "Echo"
    accept = [".txt"]
    params = [
        Param("times", "Volte", "number", default=1, min=1, max=5),
        Param("fail", "Fallisci", "bool", default=False),
        Param("slow", "Lento", "bool", default=False),
    ]
    rerun_label = "Ancora"

    def run(self, ctx: JobContext) -> dict:
        if ctx.params["fail"]:
            raise TaskError("fallito apposta")
        if ctx.params["slow"]:
            for _ in range(200):
                ctx.check_cancelled()
                time.sleep(0.02)
        text = ctx.input_path.read_text() * int(ctx.params["times"])
        (ctx.output_dir / "out.txt").write_text(text)
        return {"summary": text, "outputs": [{"file": "out.txt", "kind": "file"}]}


@pytest.fixture(scope="session")
def client():
    db.init()
    registry.discover([])
    registry.register(EchoTask())
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def users(client):
    db.create_user("admin", hash_password("password-admin"), True)
    db.create_user("mario", hash_password("password-mario"), False)
    db.create_user("luigi", hash_password("password-luigi"), False)
    return {"admin": "password-admin", "mario": "password-mario", "luigi": "password-luigi"}


@pytest.fixture
def login(client, users):
    def _login(name: str) -> dict:
        login_limiter.reset("testclient")
        r = client.post("/api/login", json={"username": name, "password": users[name]})
        assert r.status_code == 200, r.text
        client.cookies.clear()
        return {"Authorization": f"Bearer {r.json()['token']}"}
    return _login


def wait_job(client, headers, job_id, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}", headers=headers).json()
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"Il lavoro {job_id} non è terminato: {job}")


@pytest.fixture
def data_dir():
    return settings.data_dir
