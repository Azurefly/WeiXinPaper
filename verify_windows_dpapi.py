from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
VERSION = "2.1.3"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def child(operation: str, key_path: Path, value: str = "") -> dict[str, Any]:
    code = r'''
import json, os, sys
sys.path.insert(0, 'backend')
from secrets_store import decrypt, encrypt, key_storage_description
op=sys.argv[1]
value=sys.argv[2]
if op == 'encrypt':
    result={'value': encrypt(value), 'storage': key_storage_description()}
elif op == 'decrypt':
    result={'value': decrypt(value), 'storage': key_storage_description()}
else:
    raise SystemExit('invalid operation')
print(json.dumps(result))
'''
    env = {key: val for key, val in os.environ.items() if not key.startswith("STUDIO_")}
    env["STUDIO_MASTER_KEY_FILE"] = str(key_path)
    completed = subprocess.run(
        [sys.executable, "-c", code, operation, value],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(completed.stdout)


def run_validation() -> dict[str, Any]:
    if os.name != "nt":
        return {
            "product": "公众号 AI Studio",
            "version": VERSION,
            "generatedAt": utc_now(),
            "status": "skipped",
            "reason": "windows_required",
            "note": "请在 Windows 10/11 普通用户会话中运行 python verify_windows_dpapi.py。",
        }
    with tempfile.TemporaryDirectory(prefix="studio-dpapi-") as temp:
        root = Path(temp)
        raw_key = secrets.token_bytes(32)
        legacy_path = root / "legacy.key"
        legacy_path.write_bytes(raw_key)
        first = child("encrypt", legacy_path, "dpapi-legacy-migration-secret")
        stored = legacy_path.read_bytes()
        if not stored.startswith(b"dpapi:v1:"):
            raise RuntimeError("legacy raw key was not migrated to DPAPI format")
        second = child("decrypt", legacy_path, first["value"])
        if second["value"] != "dpapi-legacy-migration-secret":
            raise RuntimeError("restarted process could not decrypt migrated secret")

        new_path = root / "new.key"
        fresh = child("encrypt", new_path, "dpapi-new-key-secret")
        if not new_path.read_bytes().startswith(b"dpapi:v1:"):
            raise RuntimeError("new Windows key was not DPAPI protected")
        restarted = child("decrypt", new_path, fresh["value"])
        if restarted["value"] != "dpapi-new-key-secret":
            raise RuntimeError("restarted process could not decrypt new DPAPI secret")

        tampered = bytearray(base64.urlsafe_b64decode(first["value"].split("enc:v1:", 1)[1]))
        tampered[-1] ^= 1
        tampered_cipher = "enc:v1:" + base64.urlsafe_b64encode(bytes(tampered)).decode("ascii")
        tamper_rejected = False
        try:
            child("decrypt", legacy_path, tampered_cipher)
        except subprocess.CalledProcessError:
            tamper_rejected = True
        if not tamper_rejected:
            raise RuntimeError("tampered ciphertext was not rejected")

        return {
            "product": "公众号 AI Studio",
            "version": VERSION,
            "generatedAt": utc_now(),
            "status": "succeeded",
            "checks": {
                "legacyRawKeyMigrated": True,
                "sameUserRestartDecryptsLegacySecret": True,
                "newKeyUsesDpapi": True,
                "sameUserRestartDecryptsNewSecret": True,
                "tamperedCiphertextRejected": True,
            },
            "storage": fresh["storage"],
            "crossUserIsolation": "DPAPI CurrentUser scope is used; run this script under a second Windows account against a copied key to collect separate cross-user evidence.",
        }


def main() -> None:
    try:
        result = run_validation()
    except Exception as exc:  # noqa: BLE001
        result = {
            "product": "公众号 AI Studio",
            "version": VERSION,
            "generatedAt": utc_now(),
            "status": "failed",
            "code": exc.__class__.__name__,
            "message": str(exc)[:500],
        }
    output = json.dumps(result, ensure_ascii=False, indent=2)
    path = os.environ.get("STUDIO_DPAPI_RESULT_FILE", "").strip()
    if path:
        Path(path).write_text(output + "\n", encoding="utf-8")
    print(output)
    if result["status"] == "failed" or (
        result["status"] == "skipped" and os.environ.get("STUDIO_DPAPI_REQUIRE_WINDOWS", "") == "1"
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
