import json
import os
from pathlib import Path

import pytest

from app.config import settings
from app.services import yolo_models
from conftest import wait_job

CLASSES_DET = ["person", "desk", "chair"]


def fake_model(name, task="detect", classes=None):
    """Modello finto: basta il file e i metadati per elenchi e validazione."""
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    (settings.models_dir / name).write_bytes(b"finto")
    yolo_models.update_meta(name, task=task, classes=classes or CLASSES_DET, imgsz=640, status="pending")


@pytest.fixture(autouse=True)
def models(client):
    fake_model("det.pt")
    fake_model("cls.pt", "classify", ["pulita", "disordinata"])
    fake_model("seg.pt", "segment", ["cup", "laptop"])
    yield


def node(name, model, children=(), **kw):
    return {"id": name, "name": name, "model": model, "children": list(children), **kw}


def create(client, h, name, tree):
    return client.post("/api/pipelines", headers=h, json={"name": name, "tree": tree})


def test_crud_and_permissions(client, login):
    admin, mario = login("admin"), login("mario")
    tree = [node("Scrivanie", "det.pt", [
        node("Stato", "cls.pt", [node("Oggetti", "seg.pt", parent_classes=["disordinata"])],
             parent_classes=["desk"], padding=0.1),
    ], classes=["desk"])]
    assert create(client, mario, "vietata", tree).status_code == 403

    r = create(client, admin, "Ufficio", tree)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["node_count"] == 3 and p["version"] == 1
    assert p["models"] == ["det.pt", "cls.pt", "seg.pt"]
    child = p["tree"][0]["children"][0]
    assert child["parent_classes"] == ["desk"] and child["padding"] == 0.1

    # Gli utenti normali vedono e usano le pipeline ma non le modificano.
    assert client.get(f"/api/pipelines/{p['id']}", headers=mario).status_code == 200
    body = {"name": "Ufficio", "tree": p["tree"], "version": 1}
    assert client.put(f"/api/pipelines/{p['id']}", headers=mario, json=body).status_code == 403
    assert client.delete(f"/api/pipelines/{p['id']}", headers=mario).status_code == 403

    # Aggiunta di un fratello e salvataggio.
    p["tree"].append(node("Persone", "det.pt", classes=["person"]))
    r = client.put(f"/api/pipelines/{p['id']}", headers=admin, json={**body, "tree": p["tree"]})
    assert r.status_code == 200 and r.json()["version"] == 2 and r.json()["node_count"] == 4
    # Salvare partendo da una versione vecchia è un conflitto.
    assert client.put(f"/api/pipelines/{p['id']}", headers=admin, json=body).status_code == 409

    assert create(client, admin, "ufficio", []).status_code == 400  # nome duplicato
    assert client.delete(f"/api/pipelines/{p['id']}", headers=admin).status_code == 200


@pytest.mark.parametrize("tree,error", [
    ([], "almeno un nodo"),
    ([node("A", "nessuno.pt")], "non esiste"),
    ([node("A", "")], "scegli un modello"),
    ([node("A", "det.pt", classes=["gatto"])], "gatto"),
    ([node("A", "det.pt", [node("B", "cls.pt", parent_classes=["laptop"])])], "laptop"),
    # le classi del padre selezionabili sono solo quelle che il padre tiene
    ([node("A", "det.pt", [node("B", "cls.pt", parent_classes=["chair"])], classes=["desk"])], "chair"),
    ([node("A", "det.pt", conf=5)], "confidenza"),
    ([node("", "det.pt")], "nome"),
])
def test_validation(client, login, tree, error):
    admin = login("admin")
    r = create(client, admin, "base", [node("X", "det.pt")])
    pid = r.json()["id"]
    r = client.put(f"/api/pipelines/{pid}", headers=admin, json={"name": "base", "tree": tree, "version": 1})
    assert r.status_code == 400
    assert error in r.json()["detail"]
    client.delete(f"/api/pipelines/{pid}", headers=admin)


def test_depth_and_duplicate_ids(client, login):
    admin = login("admin")
    deep = node("L0", "det.pt")
    cur = deep
    for i in range(1, 7):
        nxt = node(f"L{i}", "det.pt")
        cur["children"] = [nxt]
        cur = nxt
    assert "profondo" in create(client, admin, "profonda", [deep]).json()["detail"]
    r = create(client, admin, "id-doppi", [node("A", "det.pt"), {**node("B", "det.pt"), "id": "A"}])
    ids = [n["id"] for n in r.json()["tree"]]
    assert len(set(ids)) == 2
    client.delete(f"/api/pipelines/{r.json()['id']}", headers=admin)


def test_model_in_use_cannot_be_deleted(client, login):
    admin = login("admin")
    fake_model("usato.pt")
    pid = create(client, admin, "usa-modello", [node("A", "usato.pt")]).json()["id"]
    r = client.delete("/api/models/usato.pt", headers=admin)
    assert r.status_code == 400 and "usa-modello" in r.json()["detail"]
    client.delete(f"/api/pipelines/{pid}", headers=admin)
    assert client.delete("/api/models/usato.pt", headers=admin).status_code == 200


def test_optimization_job_without_gpu(client, login):
    admin = login("admin")
    fake_model("da_ottimizzare.pt")
    job_id = client.post("/api/models/da_ottimizzare.pt/optimize", headers=admin).json()["job_id"]
    job = wait_job(client, admin, job_id)
    assert job["status"] == "done"
    m = next(m for m in client.get("/api/models", headers=admin).json() if m["name"] == "da_ottimizzare.pt")
    if not yolo_models.tensorrt_enabled():
        assert m["status"] == "ready" and m["backend"] == "pytorch" and m["note"]
    # Il lavoro interno non si può rieseguire né creare a mano.
    assert client.post(f"/api/jobs/{job_id}/rerun", headers=admin, json={}).status_code == 400
    r = client.post("/api/jobs", headers=admin, data={"task": "optimize_model", "params": "{}"})
    assert r.status_code == 404
    assert client.post("/api/models/nessuno.pt/optimize", headers=admin).status_code == 404


# ------------------------------------------------------------------ con modelli veri

REAL = Path(os.environ.get("JAS_TEST_MODELS", "/tmp/claude-0/smoke"))
needs_real = pytest.mark.skipif(
    not all((REAL / f).is_file() for f in ("yolo11n.pt", "yolo11n-seg.pt", "yolo11n-cls.pt")),
    reason="modelli YOLO reali non disponibili (imposta JAS_TEST_MODELS)",
)


@needs_real
def test_real_pipeline_on_image(client, login):
    import shutil

    import ultralytics

    admin = login("admin")
    for src, name in (("yolo11n.pt", "vero_det.pt"), ("yolo11n-seg.pt", "vero_seg.pt"), ("yolo11n-cls.pt", "vero_cls.pt")):
        tmp = settings.models_dir / f".upload-{name}"
        shutil.copy(REAL / src, tmp)
        yolo_models.install_model(tmp, name)

    tree = [
        node("Persone", "vero_det.pt", [
            node("Cosa indossa", "vero_cls.pt", conf=0.01, parent_classes=["person"], padding=0.1),
        ], classes=["person", "bus"]),
        node("Sagome", "vero_seg.pt", classes=["bus"]),
    ]
    p = create(client, admin, "reale", tree).json()
    image = Path(ultralytics.__file__).parent / "assets" / "bus.jpg"
    r = client.post("/api/jobs", headers=admin,
                    data={"task": "yolo_pipeline", "params": json.dumps({"pipeline": str(p["id"])})},
                    files={"file": ("bus.jpg", image.read_bytes(), "image/jpeg")})
    assert r.status_code == 200, r.text
    assert r.json()["params"]["_pipeline"]["name"] == "reale"
    job = wait_job(client, admin, r.json()["id"], timeout=120)
    assert job["status"] == "done", job["error"]

    rows = client.get(f"/api/jobs/{job['id']}/files/risultati.json", headers=admin).json()
    by_id = {r["id"]: r for r in rows}
    people = [r for r in rows if r["node"] == "Persone" and r["class"] == "person"]
    assert people, "nessuna persona trovata"
    cls_rows = [r for r in rows if r["node"] == "Cosa indossa"]
    # Una classificazione per ogni persona, collegata al suo oggetto padre e solo per le persone.
    assert len(cls_rows) == len(people)
    assert all(by_id[r["parent_id"]]["class"] == "person" for r in cls_rows)
    assert all(r["type"] == "classificazione" for r in cls_rows)
    assert any(r["node"] == "Sagome" and r["type"] == "segmentazione" for r in rows)
    # Le coordinate del figlio sono quelle del ritaglio nel frame, attorno al padre.
    for r in cls_rows:
        parent = by_id[r["parent_id"]]
        assert r["x1"] <= parent["x1"] + 1 and r["x2"] >= parent["x2"] - 1
    assert client.get(f"/api/jobs/{job['id']}/files/annotata.jpg", headers=admin).status_code == 200
    assert job["result"]["pipeline"]["name"] == "reale"
