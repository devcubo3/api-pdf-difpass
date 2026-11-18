"""Contract and isolation tests using synthetic travel documents only.

These tests exercise the public HTTP workflow, including the human approval
gate. No real traveller data, valid ticket or external AI call is required.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from pypdf import PdfReader
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.config import Settings
from app.main import create_app
from app.pipeline import process_next
from app.db import access_links
from sqlalchemy import select


TOKEN_A = "test-token-tenant-a"
TOKEN_B = "test-token-tenant-b"
TOKEN_READER = "test-token-read-only"
QR_PAYLOAD = "DIFF-TEST-000123456"


def qr_image() -> Image.Image:
    """Produce a real synthetic QR without optional rendering backends."""
    widget = QrCodeWidget(QR_PAYLOAD)
    widget.qr.make()
    count = widget.qr.getModuleCount()
    module_size, margin = 6, 4
    image = Image.new("RGB", ((count + margin * 2) * module_size,) * 2, "white")
    draw = ImageDraw.Draw(image)
    for row in range(count):
        for column in range(count):
            if widget.qr.isDark(row, column):
                left = (column + margin) * module_size
                top = (row + margin) * module_size
                draw.rectangle(
                    (left, top, left + module_size - 1, top + module_size - 1),
                    fill="black",
                )
    return image


def synthetic_pdf(*, with_qr: bool = True, note: str = "") -> bytes:
    target = io.BytesIO()
    page = canvas.Canvas(target, pagesize=(595, 842), invariant=True)
    page.setTitle("Synthetic Universal contract fixture")
    for y, line in zip(
        (790, 760, 730, 700, 670, 640),
        (
            "Universal Orlando",
            "Guest: TESTE VIAJANTE",
            "Ticket number: 000123456",
            "Valid: 2026-11-01",
            "Product: 1 Day Park Ticket",
            "Conditions: Synthetic fixture. No admission value.",
        ),
    ):
        page.drawString(40, y, line)
    if note:
        page.drawString(40, 610, note)
    if with_qr:
        page.drawImage(ImageReader(qr_image()), 40, 350, width=180, height=180)
    page.showPage()
    page.save()
    return target.getvalue()


def auth(token: str = TOKEN_A, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    data_dir = tmp_path / "private-data"
    monkeypatch.setenv("PDF_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{data_dir.as_posix()}/test.sqlite3")
    monkeypatch.setenv("PDF_REVIEW_POLICY", "always")
    monkeypatch.setenv("PDF_MAX_UPLOAD_BYTES", str(2 * 1024 * 1024))
    monkeypatch.setenv("PDF_RENDER_DPI", "120")
    monkeypatch.setenv(
        "PDF_API_TOKENS",
        json.dumps(
            {
                TOKEN_A: {
                    "tenant_id": "tenant-a",
                    "subject": "reviewer-a",
                    "roles": ["upload", "read", "review"],
                },
                TOKEN_B: {
                    "tenant_id": "tenant-b",
                    "subject": "reviewer-b",
                    "roles": ["upload", "read", "review"],
                },
                TOKEN_READER: {
                    "tenant_id": "tenant-a",
                    "subject": "reader-a",
                    "roles": ["read"],
                },
            }
        ),
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv('PDF_RENDER_MODE', 'local')
    monkeypatch.delenv("PDF_BRAND_DIR", raising=False)
    return Settings.from_env()


@pytest.fixture()
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def upload(
    client: TestClient,
    *,
    body: bytes | None = None,
    filename: str = "travel.pdf",
    token: str = TOKEN_A,
    key: str = "fixture-upload",
):
    return client.post(
        "/v2/documents",
        headers=auth(token, **{"Idempotency-Key": key}),
        files={"input_pdf": (filename, body if body is not None else synthetic_pdf(), "application/pdf")},
    )


def uploaded_id(client: TestClient, **kwargs) -> str:
    response = upload(client, **kwargs)
    assert response.status_code == 202, response.text
    return response.json()["document_id"]


def decision_body(review: dict, *, action: str = "approve", reason: str | None = None) -> dict:
    result = {
        "action": action,
        "revision": review["revision"],
        "artifact_sha256": review["artifact_sha256"],
    }
    if reason is not None:
        result["reason"] = reason
    return result


def test_health_is_public_and_upload_requires_identity(client: TestClient):
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    for headers in ({}, auth("unrecognized-token")):
        response = client.post(
            "/v2/documents",
            headers=headers,
            files={"input_pdf": ("travel.pdf", synthetic_pdf(), "application/pdf")},
        )
        assert response.status_code == 401


def test_upload_is_durable_and_original_remains_separate(client: TestClient):
    body = synthetic_pdf()
    document_id = uploaded_id(client, body=body)
    status = client.get(f"/v2/documents/{document_id}", headers=auth())
    assert status.status_code == 200
    assert status.json()["status"] == "queued"
    source = client.get(f"/v2/documents/{document_id}/source", headers=auth())
    assert source.status_code == 200
    assert source.content == body
    assert client.get(f"/v2/documents/{document_id}/pdf", headers=auth()).status_code == 409


def test_idempotency_reuses_document_but_rejects_a_different_source(client: TestClient):
    first = upload(client)
    second = upload(client)
    assert first.status_code == second.status_code == 202
    assert first.json()["document_id"] == second.json()["document_id"]
    changed = upload(client, body=synthetic_pdf(note="A distinct source revision"))
    assert changed.status_code == 409


def test_idempotency_is_scoped_to_the_authenticated_tenant(client: TestClient):
    first_id = uploaded_id(client, token=TOKEN_A, key="shared-external-key")
    second_id = uploaded_id(client, token=TOKEN_B, key="shared-external-key")
    assert first_id != second_id


@pytest.mark.parametrize("suffix", ("", "/source", "/review", "/pdf", "/preview", "/pages/1.png"))
def test_other_tenant_cannot_discover_any_document_artifact(client: TestClient, suffix: str):
    document_id = uploaded_id(client)
    response = client.get(f"/v2/documents/{document_id}{suffix}", headers=auth(TOKEN_B))
    assert response.status_code == 404


def test_read_permission_does_not_grant_upload_source_or_review(client: TestClient):
    document_id = uploaded_id(client)
    assert client.get(f"/v2/documents/{document_id}", headers=auth(TOKEN_READER)).status_code == 200
    for suffix in ("/source", "/review"):
        assert client.get(f"/v2/documents/{document_id}{suffix}", headers=auth(TOKEN_READER)).status_code == 403
    assert upload(client, token=TOKEN_READER, key="reader-upload").status_code == 403
    response = client.post(
        f"/v2/documents/{document_id}/review/decision",
        headers=auth(TOKEN_READER, **{"If-Match": '"1"'}),
        json={"action": "approve", "revision": 1, "artifact_sha256": "0" * 64},
    )
    assert response.status_code == 403


def test_another_tenant_cannot_approve_the_document(client: TestClient):
    document_id = uploaded_id(client)
    response = client.post(
        f"/v2/documents/{document_id}/review/decision",
        headers=auth(TOKEN_B, **{"If-Match": '"1"'}),
        json={"action": "approve", "revision": 1, "artifact_sha256": "0" * 64},
    )
    assert response.status_code == 404


@pytest.mark.parametrize("bad_body", (b"not a pdf", b"%PDF-1.7\nnot a complete PDF", b""))
def test_invalid_upload_is_rejected_before_acceptance(client: TestClient, bad_body: bytes):
    response = upload(client, body=bad_body)
    assert response.status_code == 400


def test_upload_limit_is_enforced(client: TestClient):
    oversized = synthetic_pdf() + b" " * (2 * 1024 * 1024)
    assert upload(client, body=oversized).status_code == 413


def test_uploaded_filename_cannot_escape_private_storage(client: TestClient, tmp_path: Path):
    body = synthetic_pdf()
    document_id = uploaded_id(client, filename="../../outside-upload.pdf", body=body)
    response = client.get(f"/v2/documents/{document_id}/source", headers=auth())
    assert response.content == body
    assert not (tmp_path / "outside-upload.pdf").exists()
    assert not (tmp_path.parent / "outside-upload.pdf").exists()
    private_root = (tmp_path / "private-data").resolve()
    for created in tmp_path.rglob("*"):
        if created.is_file():
            assert created.resolve().is_relative_to(private_root), created


def test_a_pending_job_cannot_be_approved(client: TestClient):
    document_id = uploaded_id(client)
    response = client.post(
        f"/v2/documents/{document_id}/review/decision",
        headers=auth(**{"If-Match": '"1"'}),
        json={"action": "approve", "revision": 1, "artifact_sha256": "0" * 64},
    )
    assert response.status_code == 409
    assert client.get(f"/v2/documents/{document_id}/pdf", headers=auth()).status_code == 409


def issue_link(client, document_id, artifact="pdf", token=TOKEN_A, **overrides):
    status = client.get(f"/v2/documents/{document_id}", headers=auth(token)).json()
    return client.post(f"/v2/documents/{document_id}/access-links",
        headers=auth(token, **{"If-Match": status["etag"]}),
        json={"artifact": artifact, "revision": status["revision"], **overrides})


def approve_document(client, document_id):
    review = client.get(f"/v2/documents/{document_id}/review", headers=auth()).json()
    response = client.post(f"/v2/documents/{document_id}/review/decision",
        headers=auth(**{"If-Match": review["etag"]}), json=decision_body(review))
    assert response.status_code == 200, response.text


def test_browser_access_is_scoped_expiring_and_does_not_disclose_backend_token(client, settings, monkeypatch):
    document_id = uploaded_id(client)
    assert process_next(settings)
    assert issue_link(client, document_id, artifact="pdf").status_code == 409
    assert issue_link(client, document_id, artifact="review_pdf", token=TOKEN_READER).status_code == 403
    approve_document(client, document_id)
    response = issue_link(client, document_id, token=TOKEN_READER, disposition="attachment")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["expires_in"] == 60 and response.headers["cache-control"] == "no-store"
    assert TOKEN_A not in response.text and TOKEN_READER not in response.text
    path = body["access_path"]
    assert "?" not in path
    downloaded = client.get(path)  # A browser can open the narrow capability without a Bearer key.
    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).hexdigest() == body["artifact_sha256"]
    assert "attachment" in downloaded.headers["content-disposition"]
    assert downloaded.headers["referrer-policy"] == "no-referrer"
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert client.get(path).content == downloaded.content  # PDF viewers may make several requests.
    raw_token = path.rsplit("/", 1)[1]
    with client.app.state.db.engine.connect() as connection:
        saved = dict(connection.execute(select(access_links)).mappings().one())
    assert saved["token_sha256"] == hashlib.sha256(raw_token.encode()).hexdigest()
    assert raw_token not in str(saved)
    audit = client.get(f"/v2/documents/{document_id}/review", headers=auth()).text
    assert raw_token not in audit and "access_link_issued" in audit
    assert client.get(path[:-1] + ("A" if path[-1] != "A" else "B")).status_code == 404
    monkeypatch.setattr("app.db.time.time", lambda: saved["expires_at"] + 1)
    assert client.get(path).status_code == 404


def test_review_links_open_only_the_chosen_artifact_and_expire_on_approval(client, settings):
    document_id = uploaded_id(client)
    assert process_next(settings)
    links = {kind: issue_link(client, document_id, kind, **extra) for kind, extra in (
        ("review_pdf", {}), ("preview", {}), ("page", {"page_number": 1}), ("source", {}))}
    for kind, response in links.items():
        assert response.status_code == 201, response.text
        opened = client.get(response.json()["access_path"])
        assert opened.status_code == 200
        assert opened.headers["content-type"] == ("image/png" if kind in {"preview", "page"} else "application/pdf")
    assert client.get(links["source"].json()["access_path"]).content == synthetic_pdf()
    assert issue_link(client, document_id, "page", page_number=500).status_code == 404
    approve_document(client, document_id)
    for response in links.values():
        assert client.get(response.json()["access_path"]).status_code == 404


def test_access_links_require_current_revision_auth_and_preconditions(client, settings):
    document_id = uploaded_id(client)
    route = f"/v2/documents/{document_id}/access-links"
    assert client.post(route, json={"revision": 1}).status_code == 401
    assert client.post(route, headers=auth(TOKEN_B), json={"revision": 1}).status_code == 404
    assert client.post(route, headers=auth(), json={"artifact": "source", "revision": 1}).status_code == 428
    assert client.post(route, headers=auth(**{"If-Match": '"99"'}),
        json={"artifact": "source", "revision": 1}).status_code == 409
    assert issue_link(client, document_id, "source", revision=2).status_code == 409
    for values in ({"ttl_seconds": 301}, {"ttl_seconds": 0}, {"artifact": "page"},
                   {"artifact": "source", "page_number": 1}, {"artifact": "arbitrary-path"}):
        assert issue_link(client, document_id, **values).status_code == 422
    assert process_next(settings)
    approve_document(client, document_id)
    assert issue_link(client, document_id, "source", token=TOKEN_READER).status_code == 403
    assert issue_link(client, document_id, "review_pdf", token=TOKEN_READER).status_code == 403
    assert issue_link(client, document_id, "preview", token=TOKEN_READER).status_code == 201


def test_browser_link_is_revoked_by_reprocess_or_altered_file(client, settings):
    document_id = uploaded_id(client)
    assert process_next(settings)
    approve_document(client, document_id)
    body = issue_link(client, document_id).json()
    row = client.app.state.db.get(document_id, "tenant-a")
    artifact = Path(row["artifacts_json"]["pdf_path"])
    original = artifact.read_bytes()
    artifact.write_bytes(original + b"altered")
    assert client.get(body["access_path"]).status_code == 409
    assert issue_link(client, document_id).status_code == 409
    artifact.write_bytes(original)
    response = client.post(f"/v2/documents/{document_id}/reprocess",
        headers=auth(**{"If-Match": f'"{row["etag"]}"'}), json={"reason": "Updated presentation"})
    assert response.status_code == 202
    assert client.get(body["access_path"]).status_code == 404
    assert issue_link(client, document_id, token=TOKEN_READER).status_code == 403


def test_source_capability_never_unlocks_another_document(client):
    first = uploaded_id(client)
    second = uploaded_id(client, key="different-document")
    response = issue_link(client, first, "source")
    assert response.status_code == 201
    assert client.get(f"/v2/documents/{second}/source").status_code == 401
    assert client.get(response.json()["access_path"] + "/source").status_code == 404
    assert client.get("/v2/access/short").status_code == 404


def test_access_link_survives_another_api_instance(client, settings):
    document_id = uploaded_id(client)
    body = issue_link(client, document_id, "source").json()
    with TestClient(create_app(settings)) as other_instance:
        opened = other_instance.get(body["access_path"])
        assert opened.status_code == 200
        assert hashlib.sha256(opened.content).hexdigest() == body["artifact_sha256"]


def test_link_issuance_rejects_a_concurrent_document_update(client, monkeypatch):
    document_id = uploaded_id(client)
    database = client.app.state.db
    create_link = database.create_access_link

    def changed_before_issue(row, **values):
        database.update_document(row["id"], row["tenant_id"], row["etag"], status="failed")
        return create_link(row, **values)

    monkeypatch.setattr(database, "create_access_link", changed_before_issue)
    assert issue_link(client, document_id, "source").status_code == 409
    with database.engine.connect() as connection:
        assert connection.execute(select(access_links)).mappings().all() == []
    assert database.reviews(document_id, "tenant-a") == []


def test_worker_requires_human_approval_of_the_exact_rendered_artifact(client: TestClient, settings: Settings):
    document_id = uploaded_id(client)
    assert process_next(settings) is True
    status = client.get(f"/v2/documents/{document_id}", headers=auth()).json()
    assert status["status"] == "needs_review", status
    assert client.get(f"/v2/documents/{document_id}/pdf", headers=auth()).status_code == 409
    response = client.get(f"/v2/documents/{document_id}/review", headers=auth())
    assert response.status_code == 200, response.text
    review = response.json()
    assert len(review["artifact_sha256"]) == 64
    payload = decision_body(review)
    endpoint = f"/v2/documents/{document_id}/review/decision"

    missing_precondition = client.post(endpoint, headers=auth(), json=payload)
    assert missing_precondition.status_code == 428
    wrong_artifact = client.post(
        endpoint,
        headers=auth(**{"If-Match": review["etag"]}),
        json={**payload, "artifact_sha256": "0" * 64},
    )
    assert wrong_artifact.status_code == 409
    assert client.get(f"/v2/documents/{document_id}/pdf", headers=auth()).status_code == 409

    approved = client.post(endpoint, headers=auth(**{"If-Match": review["etag"]}), json=payload)
    assert approved.status_code == 200, approved.text
    current = client.get(f"/v2/documents/{document_id}", headers=auth()).json()
    assert current["status"] == "ready", current
    delivered = client.get(f"/v2/documents/{document_id}/pdf", headers=auth(TOKEN_READER))
    assert delivered.status_code == 200
    assert delivered.headers["content-type"].startswith("application/pdf")
    assert hashlib.sha256(delivered.content).hexdigest() == review["artifact_sha256"]
    assert delivered.content != synthetic_pdf()
    reader = PdfReader(io.BytesIO(delivered.content))
    assert len(reader.pages) >= 1
    assert not reader.attachments
    expected_code = qr_image()
    assert any(
        item.image.size == expected_code.size
        and item.image.convert("RGB").tobytes() == expected_code.tobytes()
        for page in reader.pages
        for item in page.images
    ), "The original synthetic QR pixels must survive in the delivered PDF"


def test_stale_approval_cannot_overwrite_a_review_decision(client: TestClient, settings: Settings):
    document_id = uploaded_id(client)
    assert process_next(settings) is True
    review = client.get(f"/v2/documents/{document_id}/review", headers=auth()).json()
    endpoint = f"/v2/documents/{document_id}/review/decision"
    rejection = client.post(
        endpoint,
        headers=auth(**{"If-Match": review["etag"]}),
        json=decision_body(review, action="reject", reason="Synthetic correction required"),
    )
    assert rejection.status_code == 200, rejection.text
    stale = client.post(endpoint, headers=auth(**{"If-Match": review["etag"]}), json=decision_body(review))
    assert stale.status_code in (409, 412)
    assert client.get(f"/v2/documents/{document_id}/pdf", headers=auth()).status_code == 409


def test_review_cannot_edit_a_code_or_arbitrary_path(client: TestClient, settings: Settings):
    document_id = uploaded_id(client)
    assert process_next(settings) is True
    review = client.get(f"/v2/documents/{document_id}/review", headers=auth()).json()
    response = client.patch(
        f"/v2/documents/{document_id}/review",
        headers=auth(**{"If-Match": review["etag"]}),
        json={
            "changes": {"codes[0].payload": "FAKE-REPLACEMENT", "storage_path": "../../other.pdf"},
            "reason": "Synthetic forbidden edit",
        },
    )
    assert response.status_code in (400, 422)
    after = client.get(f"/v2/documents/{document_id}/review", headers=auth()).json()
    assert after["etag"] == review["etag"]
    assert after["artifact_sha256"] == review["artifact_sha256"]


def test_correction_creates_a_new_revision_and_requires_fresh_artifact_approval(
    client: TestClient, settings: Settings
):
    document_id = uploaded_id(client)
    assert process_next(settings) is True
    review_url = f"/v2/documents/{document_id}/review"
    decision_url = f"{review_url}/decision"
    first = client.get(review_url, headers=auth()).json()
    correction = {
        "changes": {"title": "Universal Orlando | Revisao 2"},
        "reason": "Synthetic presentation correction approved by the operator",
    }
    assert client.patch(review_url, headers=auth(), json=correction).status_code == 428
    changed = client.patch(
        review_url,
        headers=auth(**{"If-Match": first["etag"]}),
        json=correction,
    )
    assert changed.status_code in (200, 202), changed.text
    queued = client.get(f"/v2/documents/{document_id}", headers=auth()).json()
    assert queued["status"] == "queued"
    assert queued["revision"] == first["revision"] + 1
    stale_correction = client.patch(
        review_url,
        headers=auth(**{"If-Match": first["etag"]}),
        json=correction,
    )
    assert stale_correction.status_code in (409, 412)
    stale_approval = client.post(
        decision_url,
        headers=auth(**{"If-Match": first["etag"]}),
        json=decision_body(first),
    )
    assert stale_approval.status_code in (409, 412)
    assert client.get(f"/v2/documents/{document_id}/pdf", headers=auth()).status_code == 409

    assert process_next(settings) is True
    updated = client.get(review_url, headers=auth()).json()
    assert updated["revision"] == first["revision"] + 1
    assert updated["artifact_sha256"] != first["artifact_sha256"]
    assert updated["etag"] != first["etag"]
    assert updated["data"]["overrides"]["title"] == correction["changes"]["title"]
    invalid_previous_hash = client.post(
        decision_url,
        headers=auth(**{"If-Match": updated["etag"]}),
        json={**decision_body(updated), "artifact_sha256": first["artifact_sha256"]},
    )
    assert invalid_previous_hash.status_code == 409
    approved = client.post(
        decision_url,
        headers=auth(**{"If-Match": updated["etag"]}),
        json=decision_body(updated),
    )
    assert approved.status_code == 200, approved.text
    delivered = client.get(f"/v2/documents/{document_id}/pdf", headers=auth())
    assert delivered.status_code == 200
    assert hashlib.sha256(delivered.content).hexdigest() == updated["artifact_sha256"]


def test_approval_detects_changed_pdf_bytes(client: TestClient, settings: Settings):
    document_id = uploaded_id(client)
    process_next(settings)
    review = client.get(f'/v2/documents/{document_id}/review', headers=auth()).json()
    row = client.app.state.db.get(document_id, 'tenant-a')
    Path(row['artifacts_json']['pdf_path']).write_bytes(b'changed-after-validation')
    result = client.post(f'/v2/documents/{document_id}/review/decision',
        headers=auth(**{'If-Match':review['etag']}), json=decision_body(review))
    assert result.status_code == 409
    assert client.get(f'/v2/documents/{document_id}/pdf', headers=auth()).status_code == 409


def test_approved_revision_remains_available_after_correction(client: TestClient, settings: Settings):
    document_id = uploaded_id(client)
    process_next(settings)
    review_url = f'/v2/documents/{document_id}/review'
    review = client.get(review_url, headers=auth()).json()
    approved = client.post(review_url+'/decision', headers=auth(**{'If-Match':review['etag']}),
                           json=decision_body(review))
    assert approved.status_code == 200
    previous = client.get(f'/v2/documents/{document_id}/pdf', headers=auth()).content
    updated = client.get(review_url, headers=auth()).json()
    result = client.patch(review_url, headers=auth(**{'If-Match':updated['etag']}),
                          json={'changes':{'title':'Revisão seguinte'},'reason':'Teste da revisão imutável'})
    assert result.status_code == 202
    assert client.get(f'/v2/documents/{document_id}/pdf', headers=auth()).status_code == 409
    historical = client.get(f'/v2/documents/{document_id}/pdf?revision=1', headers=auth(TOKEN_READER))
    assert historical.status_code == 200 and historical.content == previous


def test_unknown_format_is_not_approved_by_a_human_override(client: TestClient, settings: Settings):
    source = io.BytesIO()
    page = canvas.Canvas(source)
    page.drawString(40, 700, 'Unsupported supplier: important travel conditions here.')
    page.save()
    document_id = uploaded_id(client, body=source.getvalue())
    process_next(settings)
    review = client.get(f'/v2/documents/{document_id}/review', headers=auth()).json()
    assert review['status'] == 'needs_review'
    assert any(issue['code'] == 'UNSUPPORTED_FORMAT' for issue in review['issues'])
    result = client.post(f'/v2/documents/{document_id}/review/decision',
        headers=auth(**{'If-Match':review['etag']}), json=decision_body(review))
    assert result.status_code == 409


def test_missing_ocr_keeps_source_and_explicit_review_reason(client: TestClient, settings: Settings, monkeypatch):
    import pytesseract
    from app import extraction
    def unavailable(_page):
        raise pytesseract.TesseractNotFoundError()
    monkeypatch.setattr(extraction, '_ocr', unavailable)
    source = io.BytesIO()
    page = canvas.Canvas(source)
    page.drawImage(ImageReader(Image.new('RGB', (300, 100), 'navy')), 40, 400, width=300, height=100)
    page.save()
    document_id = uploaded_id(client, body=source.getvalue())
    process_next(settings)
    review = client.get(f'/v2/documents/{document_id}/review', headers=auth()).json()
    assert review['status'] == 'needs_review'
    assert any(issue['code'] == 'OCR_UNAVAILABLE' and issue['blocking'] for issue in review['issues'])
    assert client.get(f'/v2/documents/{document_id}/source', headers=auth()).content == source.getvalue()
    rejected = client.post(f'/v2/documents/{document_id}/review/decision',
        headers=auth(**{'If-Match':review['etag']}),
        json={'action':'reject','revision':review['revision'],'reason':'Solicitar fonte legível'})
    assert rejected.status_code == 200


def test_premium_api_has_no_original_api_entrypoint(client: TestClient, monkeypatch):
    # Even a stale environment flag cannot enable the other project's contract.
    monkeypatch.setenv('PDF_ALLOW_LEGACY', 'true')
    with TestClient(create_app(Settings.from_env())) as isolated:
        spec = isolated.get('/openapi.json').json()
        assert spec['info']['title'] == 'Diff Travel Premium PDF API'
        assert '/generate-pdf/' not in spec['paths']
        assert isolated.post('/generate-pdf/', headers=auth()).status_code == 404
