import json

from app.security import login_limiter
from conftest import wait_job


def submit(client, headers, text="ciao", **params):
    return client.post(
        "/api/jobs",
        headers=headers,
        data={"task": "echo", "params": json.dumps(params)},
        files={"file": ("prova.txt", text.encode(), "text/plain")},
    )


def test_requires_login(client):
    client.cookies.clear()
    assert client.get("/api/tasks").status_code == 401
    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 200


def test_wrong_password_and_rate_limit(client, users):
    login_limiter.reset("testclient")
    for _ in range(5):
        r = client.post("/api/login", json={"username": "mario", "password": "sbagliata"})
        assert r.status_code == 401
    r = client.post("/api/login", json={"username": "mario", "password": users["mario"]})
    assert r.status_code == 429
    login_limiter.reset("testclient")


def test_cookie_login_and_logout(client, users):
    login_limiter.reset("testclient")
    client.cookies.clear()
    r = client.post("/api/login", json={"username": "MARIO", "password": users["mario"]})
    assert r.status_code == 200
    assert client.get("/api/me").json()["username"] == "mario"
    client.post("/api/logout")
    client.cookies.clear()
    assert client.get("/api/me").status_code == 401


def test_tasks_listed(client, login):
    tasks = {t["id"]: t for t in client.get("/api/tasks", headers=login("mario")).json()}
    assert {"echo", "yolo_detect", "pdf_qa"} <= set(tasks)
    # Senza Ollama il compito PDF è segnalato come non disponibile.
    assert tasks["pdf_qa"]["available"] is False
    model_param = next(p for p in tasks["yolo_detect"]["params"] if p["name"] == "model")
    assert "yolo11n.pt" in model_param["choices"]


def test_job_flow(client, login):
    h = login("mario")
    r = submit(client, h, "abc", times=3)
    assert r.status_code == 200, r.text
    job = wait_job(client, h, r.json()["id"])
    assert job["status"] == "done"
    assert job["result"]["summary"] == "abcabcabc"
    f = client.get(f"/api/jobs/{job['id']}/files/out.txt", headers=h)
    assert f.text == "abcabcabc"
    assert client.get(f"/api/jobs/{job['id']}/input", headers=h).text == "abc"


def test_validation(client, login):
    h = login("mario")
    assert submit(client, h, times=99).status_code == 400
    r = client.post("/api/jobs", headers=h, data={"task": "echo", "params": "{}"},
                    files={"file": ("x.exe", b"x", "application/octet-stream")})
    assert r.status_code == 400
    r = client.post("/api/jobs", headers=h, data={"task": "echo", "params": "{}"})
    assert r.status_code == 400
    assert client.post("/api/jobs", headers=h, data={"task": "nessuno"}).status_code == 404


def test_failed_job_shows_error(client, login):
    h = login("mario")
    job = wait_job(client, h, submit(client, h, fail=True).json()["id"])
    assert job["status"] == "failed"
    assert job["error"] == "fallito apposta"


def test_cancel_running_job(client, login):
    h = login("mario")
    job_id = submit(client, h, slow=True).json()["id"]
    for _ in range(100):
        if client.get(f"/api/jobs/{job_id}", headers=h).json()["status"] == "running":
            break
    assert client.post(f"/api/jobs/{job_id}/cancel", headers=h).status_code == 200
    assert wait_job(client, h, job_id)["status"] == "cancelled"


def test_rerun_reuses_file(client, login):
    h = login("mario")
    first = wait_job(client, h, submit(client, h, "xy").json()["id"])
    r = client.post(f"/api/jobs/{first['id']}/rerun", headers=h, json={"params": {"times": 2}})
    assert r.status_code == 200, r.text
    second = wait_job(client, h, r.json()["id"])
    assert second["result"]["summary"] == "xyxy"
    assert second["input_name"] == "prova.txt"


def test_users_are_isolated(client, login):
    mario = login("mario")
    job_id = wait_job(client, mario, submit(client, mario).json()["id"])["id"]
    luigi = login("luigi")
    assert client.get(f"/api/jobs/{job_id}", headers=luigi).status_code == 404
    assert client.get(f"/api/jobs/{job_id}/files/out.txt", headers=luigi).status_code == 404
    assert all(j["id"] != job_id for j in client.get("/api/jobs?all=true", headers=luigi).json())
    admin = login("admin")
    assert client.get(f"/api/jobs/{job_id}", headers=admin).status_code == 200
    assert any(j["id"] == job_id for j in client.get("/api/jobs?all=true", headers=admin).json())


def test_path_traversal_blocked(client, login):
    h = login("mario")
    job_id = wait_job(client, h, submit(client, h).json()["id"])["id"]
    for name in ("..%2F..%2Fserver.db", "../input/prova.txt", "%2E%2E"):
        assert client.get(f"/api/jobs/{job_id}/files/{name}", headers=h).status_code == 404


def test_delete_job(client, login, data_dir):
    h = login("mario")
    job_id = wait_job(client, h, submit(client, h).json()["id"])["id"]
    assert (data_dir / "jobs" / job_id).is_dir()
    assert client.delete(f"/api/jobs/{job_id}", headers=h).status_code == 200
    assert not (data_dir / "jobs" / job_id).exists()
    assert client.get(f"/api/jobs/{job_id}", headers=h).status_code == 404


def test_admin_only_endpoints(client, login):
    mario = login("mario")
    assert client.get("/api/users", headers=mario).status_code == 403
    assert client.post("/api/models", headers=mario, files={"file": ("a.pt", b"x")}).status_code == 403
    admin = login("admin")
    r = client.post("/api/users", headers=admin, json={"username": "nuovo", "password": "12345678"})
    assert r.status_code == 200
    assert client.post("/api/users", headers=admin, json={"username": "x", "password": "12345678"}).status_code == 400
    assert client.post("/api/users", headers=admin, json={"username": "corto", "password": "123"}).status_code == 400
    uid = r.json()["id"]
    assert client.delete(f"/api/users/{uid}", headers=admin).status_code == 200


def test_invalid_model_upload_rejected(client, login, data_dir):
    admin = login("admin")
    r = client.post("/api/models", headers=admin, files={"file": ("finto.pt", b"non un modello")})
    assert r.status_code == 400
    assert not list((data_dir / "models" / "yolo").glob("*finto*"))
    assert client.post("/api/models", headers=admin, files={"file": ("../x.pt", b"x")}).status_code in (400, 422)


def test_change_password(client, login, users):
    h = login("luigi")
    assert client.post("/api/me/password", headers=h,
                       json={"current_password": "sbagliata", "new_password": "nuova-password"}).status_code == 400
    assert client.post("/api/me/password", headers=h,
                       json={"current_password": users["luigi"], "new_password": "nuova-password"}).status_code == 200
    # Le sessioni precedenti vengono chiuse.
    assert client.get("/api/me", headers=h).status_code == 401
    users["luigi"] = "nuova-password"
    assert client.get("/api/me", headers=login("luigi")).status_code == 200


def test_system_status(client, login):
    s = client.get("/api/system", headers=login("mario")).json()
    assert s["ram_total_gb"] > 0
    assert s["ollama"]["running"] is False
