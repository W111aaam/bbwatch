"""LGUide cloud sync adapter.

Blackboard credentials and cookies never leave the user's machine. This module
exports only the normalized bbwatch SQLite snapshot and authenticates with a
dedicated, revocable LGUide sync token stored in the OS keyring.
"""
from __future__ import annotations

import getpass
import json
import sqlite3
from pathlib import Path
from urllib import error, request

import keyring
from keyring import errors as keyring_errors

SERVICE = "bbwatch-lguide"
TOKEN_KEY = "sync-token"
URL_KEY = "base-url"
DEFAULT_URL = "https://lguide.net"


def store_connection(token: str, base_url: str = DEFAULT_URL) -> None:
    token = token.strip()
    if not token.startswith("lgbb_"):
        raise ValueError("无效的 LGUide 同步令牌（应以 lgbb_ 开头）")
    url = base_url.strip().rstrip("/")
    if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
        raise ValueError("LGUide 地址必须使用 HTTPS")
    try:
        keyring.set_password(SERVICE, TOKEN_KEY, token)
        keyring.set_password(SERVICE, URL_KEY, url)
    except keyring_errors.KeyringError as exc:
        raise RuntimeError("无法将 LGUide 同步令牌保存到系统钥匙串") from exc


def load_connection() -> tuple[str, str] | None:
    try:
        token = keyring.get_password(SERVICE, TOKEN_KEY)
        if not token:
            return None
        return token, keyring.get_password(SERVICE, URL_KEY) or DEFAULT_URL
    except keyring_errors.KeyringError as exc:
        raise RuntimeError("无法读取 LGUide 同步令牌") from exc


def clear_connection() -> None:
    for key in (TOKEN_KEY, URL_KEY):
        try:
            keyring.delete_password(SERVICE, key)
        except keyring_errors.PasswordDeleteError:
            pass
        except keyring_errors.KeyringError as exc:
            raise RuntimeError("无法清除 LGUide 同步令牌") from exc


def prompt_and_store(base_url: str = DEFAULT_URL, token: str | None = None) -> None:
    value = token or getpass.getpass("LGUide 同步令牌（输入不回显）: ")
    store_connection(value, base_url)


def build_snapshot(db_path: str | Path) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        entities = conn.execute(
            "SELECT entity_key, kind, course_id, due_utc, grade_status, grade_score, "
            "payload_json FROM seen_entity WHERE archived=0"
        ).fetchall()
        overrides = {
            row["entity_key"]: bool(row["manual_done"])
            for row in conn.execute("SELECT entity_key, manual_done FROM task_override")
        }
        hidden = {row["entity_key"] for row in conn.execute("SELECT entity_key FROM task_hidden")}
        downloads = conn.execute(
            "SELECT att_key, course_id, local_path, src_modified_utc, status FROM download"
        ).fetchall()
        last_scan = conn.execute(
            "SELECT finished_at FROM scan_run WHERE finished_at IS NOT NULL "
            "AND status IN ('ok', 'partial', 'success') ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    course_names: dict[str, str] = {}
    parsed: list[tuple[sqlite3.Row, dict]] = []
    for row in entities:
        payload = json.loads(row["payload_json"] or "{}")
        parsed.append((row, payload))
        label = payload.get("course_code") or payload.get("course_name")
        if label:
            course_names[row["course_id"]] = label

    tasks, announcements, materials = [], [], []
    material_by_content: dict[tuple[str, str], dict] = {}
    for row, payload in parsed:
        course_id = row["course_id"]
        course_name = course_names.get(course_id) or course_id
        if row["kind"] == "column" and row["due_utc"] and row["entity_key"] not in hidden:
            if payload.get("score_possible") == 0:
                continue
            status = row["grade_status"] or "None"
            done = status in ("NeedsGrading", "Graded") or row["grade_score"] is not None
            tasks.append({
                "entity_key": row["entity_key"], "course_id": course_id,
                "course_name": course_name, "title": payload.get("name") or "Untitled",
                "due_utc": row["due_utc"], "status": "graded" if status == "Graded" else ("submitted" if done else "todo"),
                "score": row["grade_score"], "content_id": payload.get("content_id"),
                "manual_done": overrides.get(row["entity_key"], False),
            })
        elif row["kind"] == "announcement":
            announcements.append({
                "entity_key": row["entity_key"], "course_id": course_id,
                "course_name": course_name, "title": payload.get("title") or "Untitled",
                "body": payload.get("body") or "", "created_utc": payload.get("created"),
            })
        elif row["kind"] == "content" and payload.get("handler") != "resource/x-bb-folder":
            content_id = row["entity_key"].rsplit(":", 1)[-1]
            item = {
                "entity_key": row["entity_key"], "course_id": course_id,
                "course_name": course_name, "title": payload.get("title") or "Untitled",
                "handler": payload.get("handler"), "modified_utc": payload.get("modified"),
                "local_path": None, "downloaded": False,
            }
            materials.append(item)
            material_by_content[(course_id, content_id)] = item

    for row in downloads:
        parts = row["att_key"].split(":", 3)
        content_id = parts[2] if len(parts) == 4 else ""
        item = material_by_content.get((row["course_id"], content_id))
        if item and row["status"] == "done":
            item["downloaded"] = True
            item["local_path"] = row["local_path"]
            item["modified_utc"] = item["modified_utc"] or row["src_modified_utc"]

    return {
        "schema_version": 1,
        "scanned_at": last_scan["finished_at"] if last_scan else None,
        "courses": [
            {"course_id": cid, "course_name": name}
            for cid, name in sorted(course_names.items(), key=lambda item: item[1])
        ],
        "tasks": sorted(tasks, key=lambda item: item["due_utc"] or "9999"),
        "announcements": sorted(announcements, key=lambda item: item["created_utc"] or "", reverse=True),
        "materials": sorted(materials, key=lambda item: item["modified_utc"] or "", reverse=True),
    }


def upload_snapshot(db_path: str | Path, token: str, base_url: str, timeout: int = 30) -> dict:
    body = json.dumps(build_snapshot(db_path), ensure_ascii=False).encode("utf-8")
    req = request.Request(
        f"{base_url.rstrip('/')}/api/blackboard/sync",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error")
        except Exception:  # noqa: BLE001
            detail = None
        raise RuntimeError(detail or f"LGUide 同步失败：HTTP {exc.code}") from exc
    except error.URLError as exc:
        raise RuntimeError("无法连接 LGUide 同步服务") from exc


def sync_if_configured(db_path: str | Path) -> dict | None:
    connection = load_connection()
    if not connection:
        return None
    token, base_url = connection
    return upload_snapshot(db_path, token, base_url)
