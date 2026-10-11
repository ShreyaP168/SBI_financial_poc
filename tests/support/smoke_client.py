"""Regress the running services across synthetic decisions and delivery blocks."""

import json
import os
import time
import urllib.error
import urllib.request
import uuid


def main():
    base_url = os.environ["UW_TEST_GATEWAY_URL"]
    token = os.environ["UW_GATEWAY_TOKEN"]

    def request(path, *, body=None, authorized=True, key=None, review=False):
        headers = {"Content-Type": "application/json"}
        if authorized:
            credential = os.environ["UW_TEST_REVIEWER_TOKEN"] if review else token
            headers["Authorization"] = f"Bearer {credential}"
        if key:
            headers["Idempotency-Key"] = key
        data = json.dumps(body).encode() if body is not None else None
        call = urllib.request.Request(base_url + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(call, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as response:
            return response.code, response.read()

    assert request("/health/ready")[0] == 200
    assert request("/v1/runs", body={"case_id": "SYNTHETIC"}, authorized=False)[0] == 401
    assert request("/v1/runs", body={"case_id": "SYNTHETIC"})[0] == 422
    key = str(uuid.uuid4())
    body = {"case_id": "SYNTHETIC", "proposed_sa": 10000000}
    code, content = request("/v1/runs", body=body, key=key)
    assert code == 202, content
    job_id = json.loads(content)["job_id"]
    repeated_code, repeated = request("/v1/runs", body=body, key=key)
    assert repeated_code == 202 and json.loads(repeated)["job_id"] == job_id
    assert request("/v1/runs", body={"case_id": "OTHER"}, key=key)[0] == 409
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        code, content = request(f"/v1/runs/{job_id}")
        assert code == 200
        job = json.loads(content)
        if job["status"] in {"succeeded", "incomplete", "failed"}:
            break
        time.sleep(0.5)
    else:
        raise AssertionError("Workflow did not finish within 60 seconds")
    assert job["status"] == "succeeded", job
    assert job["result"]["verdict"] == "DECLINE", job
    assert job["result"]["artifact"]["qc_status"] in {"PASS", "WARN"}, job
    code, document = request(f"/v1/runs/{job_id}/case-sheet")
    assert code == 200 and document.startswith(b"%PDF"), code
    print(
        json.dumps(
            {
                "test": "docker-service-workflow",
                "status": "passed",
                "database": "PostgreSQL",
                "verdict": "DECLINE",
                "pdf_bytes": len(document),
                "qc": job["result"]["artifact"]["qc_status"],
                "provider_calls": 0,
            }
        )
    )
    scenarios = [
        ("E2E_FR", "FURTHER_REQUIREMENT", 10000000, True),
        ("E2E_REFER", "REFER", 2000000, True),
        ("E2E_KIRANA", "DECLINE", 10000000, True),
        ("E2E_STUDENT_ADB", "DECLINE", 10000000, True),
        ("E2E_INCOMPLETE", "INCOMPLETE", 10000000, False),
    ]
    for case_id, expected, sa, deliverable in scenarios:
        started = time.monotonic()
        body = {"case_id": case_id, "proposed_sa": sa}
        key = str(uuid.uuid4())
        code, content = request("/v1/runs", body=body, key=key)
        assert code == 202, (case_id, code)
        job_id = json.loads(content)["job_id"]
        retry_code, retry = request("/v1/runs", body=body, key=key)
        assert retry_code == 202 and json.loads(retry)["job_id"] == job_id
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            code, content = request(f"/v1/runs/{job_id}")
            assert code == 200
            job = json.loads(content)
            if job["status"] in {"succeeded", "incomplete", "failed"}:
                break
            time.sleep(0.5)
        else:
            raise AssertionError(f"{case_id} exceeded workflow timeout")
        assert job["status"] == ("succeeded" if deliverable else "incomplete"), job
        assert job["result"]["verdict"] == expected, job
        code, document = request(f"/v1/runs/{job_id}/case-sheet")
        assert code == (200 if deliverable else 409), (case_id, code)
        if deliverable:
            assert document.startswith(b"%PDF")
        else:
            review_path = f"/v1/runs/{job_id}/review-case-sheet"
            assert request(review_path)[0] == 401
            review_code, review_document = request(review_path, review=True)
            assert review_code == 200 and review_document.startswith(b"%PDF")
        print(
            json.dumps(
                {
                    "test": "docker-e2e-scenario",
                    "status": "passed",
                    "case": case_id,
                    "verdict": expected,
                    "download_status": code,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "provider_calls": 0,
                }
            )
        )
    code, metrics = request("/v1/operations/queue")
    assert code == 200 and json.loads(metrics)["pending"] == 0


if __name__ == "__main__":
    main()
