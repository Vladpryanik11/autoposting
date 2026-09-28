"""Клиент Postmypost REST API v4.1 (https://help.postmypost.io/docs/api)."""
import os
import time
from datetime import datetime

import requests

BASE_URL = "https://api.postmypost.io/v4.1"

# publication_type
POST, STORY, REELS = 1, 2, 4
# publication_status при создании
DRAFT, PENDING = 4, 5
# publication_status в ответах
ST_DELETED, ST_PUBLISHED, ST_PUBLISHING, ST_ERROR, ST_DRAFT, ST_PENDING, ST_NOT_DELETED = 0, 1, 2, 3, 4, 5, 6
# статусы загрузки файла
UPLOAD_OK, UPLOAD_ERROR = 1, 2
# delete_option: 3 — удалить только в Postmypost (пост ещё не вышел)
DELETE_ONLY_PMP = 3


class PostmypostError(RuntimeError):
    pass


def platform_details(platform: str | None) -> dict:
    """Параметры публикации, специфичные для площадки."""
    if platform == "youtube":
        return {"youtube_privacy_status": 1}             # public
    if platform == "tiktok":
        return {"tiktok_privacy_status": 1, "tiktok_comment": True}  # public
    if platform == "instagram":
        return {"instagram_share_to_feed": True}
    return {}


class Postmypost:
    def __init__(self, token: str, session: requests.Session | None = None):
        if not token:
            raise PostmypostError("Не задан POSTMYPOST_TOKEN в .env")
        self.s = session or requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def _req(self, method, path, **kw):
        delay, err = 2, ""
        for _ in range(6):
            try:
                r = self.s.request(method, BASE_URL + path, timeout=60, **kw)
            except requests.RequestException as e:
                err = str(e)
            else:
                if r.status_code == 429:  # лимит: 10/мин на upload и publications, 30/мин на остальное
                    time.sleep(int(r.headers.get("Retry-After", 10)) + 1)
                    continue
                if r.status_code < 400:
                    return r.json() if r.content else {}
                if r.status_code < 500:
                    raise PostmypostError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
                err = f"{r.status_code}: {r.text[:200]}"
            time.sleep(delay)
            delay = min(delay * 2, 60)
        raise PostmypostError(f"{method} {path}: не отвечает ({err})")

    # --- справочники ---
    def projects(self):
        return self._req("GET", "/projects", params={"per_page": 50}).get("data", [])

    def accounts(self, project_id: int):
        return self._req("GET", "/accounts", params={"project_id": project_id, "per_page": 50}).get("data", [])

    def channels(self):
        return self._req("GET", "/channels", params={"per_page": 50}).get("data", [])

    # --- загрузка файла: init -> S3 presigned POST -> complete -> status ---
    def upload(self, project_id: int, path, poll_timeout: int = 900) -> int:
        """Загружает локальный файл, возвращает file_id для публикации."""
        path = str(path)
        init = self._req("POST", "/upload/init", json={
            "project_id": project_id,
            "name": os.path.basename(path),
            "size": os.path.getsize(path),
        })
        fields = [(f["key"], f["value"]) for f in init["fields"]]
        with open(path, "rb") as fh:
            try:
                r = requests.post(init["action"], data=fields,
                                  files={"file": (os.path.basename(path), fh)}, timeout=1800)
            except requests.RequestException as e:
                raise PostmypostError(f"S3 upload: {e}") from e
        if r.status_code not in (200, 201, 204, 302):
            raise PostmypostError(f"S3 upload -> {r.status_code}: {r.text[:500]}")

        upload_id = init["id"]
        self._req("POST", "/upload/complete", params={"id": upload_id})

        deadline = time.time() + poll_timeout
        while time.time() < deadline:
            time.sleep(7)  # не выходим за 10 запросов/мин на /upload/*
            st = self._req("GET", "/upload/status", params={"id": upload_id})
            if st.get("status") == UPLOAD_OK and st.get("file_id"):
                return st["file_id"]
            if st.get("status") == UPLOAD_ERROR:
                raise PostmypostError(f"Обработка файла упала: {st}")
        raise PostmypostError(f"Файл {path} не обработался за {poll_timeout} c")

    # --- публикации ---
    def publish(self, project_id: int, account_ids: list[int], file_ids: list[int],
                text: str = "", title: str = "", post_at: datetime | None = None,
                publication_type: int = REELS, status: int = PENDING,
                platform: str | None = None) -> dict:
        post_at = post_at or datetime.now().astimezone()
        detail = {"publication_type": publication_type, "content": text, "file_ids": file_ids,
                  **platform_details(platform)}
        if len(account_ids) == 1:
            detail["account_id"] = account_ids[0]
        if title:
            detail["title"] = title
        return self._req("POST", "/publications", json={
            "project_id": project_id,
            "post_at": post_at.isoformat(timespec="seconds"),
            "account_ids": account_ids,
            "publication_status": status,
            "details": [detail],
        })

    def publication(self, publication_id: int) -> dict:
        data = self._req("GET", f"/publications/{publication_id}")
        return data.get("data", data) if isinstance(data, dict) else data

    def find_publication(self, project_id: int, post_at: datetime, account_id: int) -> int | None:
        """Ищет уже созданную публикацию (на случай сбоя после создания, но до сохранения id)."""
        data = self._req("GET", "/publications", params={
            "project_id": project_id, "post_date": post_at.astimezone().date().isoformat(), "per_page": 50})
        for p in data.get("data", []):
            try:
                same_time = abs((datetime.fromisoformat(p["post_at"]) - post_at).total_seconds()) < 60
            except (KeyError, TypeError, ValueError):
                continue
            if same_time and account_id in (p.get("account_ids") or []):
                return p["id"]
        return None

    def published_posts(self, project_id: int, account_id: int, date_from, date_to) -> list[dict]:
        """Вышедшие посты аккаунта (из аналитики): external_id, external_url, created_at, content.
        Для Instagram external_url — ссылка на профиль (проверено 27.09), для других сетей — может быть на пост."""
        data = self._req("GET", "/analytics/publications", params={
            "project_id": project_id, "account_id": str(account_id), "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(), "per_page": 50, "sort": "-created_at"})
        return data.get("data", []) if isinstance(data, dict) else []

    def delete_publication(self, publication_id: int, account_ids: list[int],
                           delete_option: int = DELETE_ONLY_PMP) -> None:
        # account_ids обязателен (проверено на живом API), передаётся строкой через запятую
        self._req("DELETE", f"/publications/{publication_id}",
                  params={"delete_option": delete_option, "account_ids": ",".join(map(str, account_ids))})
