"""Клиент Apify REST API v2: запуск актора, ожидание, чтение датасета."""
import time

import requests

BASE_URL = "https://api.apify.com/v2"
TERMINAL = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}

# Цена apify/instagram-reel-scraper на тарифе FREE (самая высокая) — для потолка расходов.
REEL_PRICE_USD = 0.0026
START_PRICE_USD = 0.001


class ApifyError(RuntimeError):
    pass


def cost_cap(reels: int) -> float:
    """Потолок стоимости запуска: столько, сколько стоят `reels` роликов, +10 %."""
    return round(START_PRICE_USD + reels * REEL_PRICE_USD * 1.1 + 0.01, 4)


class Apify:
    def __init__(self, token: str, session: requests.Session | None = None):
        if not token:
            raise ApifyError("Не задан APIFY_TOKEN в .env")
        self.s = session or requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def _req(self, method: str, path: str, **kw):
        delay = 2
        for attempt in range(6):
            try:
                r = self.s.request(method, BASE_URL + path, timeout=90, **kw)
            except requests.RequestException as e:
                err = str(e)
            else:
                if r.status_code < 400:
                    return r.json()
                if r.status_code != 429 and r.status_code < 500:
                    raise ApifyError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
                err = f"{r.status_code}: {r.text[:200]}"
            time.sleep(delay)
            delay = min(delay * 2, 60)
        raise ApifyError(f"{method} {path}: не отвечает ({err})")

    def start_run(self, actor: str, run_input: dict, max_total_charge_usd: float) -> dict:
        return self._req("POST", f"/acts/{actor}/runs", json=run_input,
                         params={"maxTotalChargeUsd": max_total_charge_usd})["data"]

    def wait_run(self, run_id: str, timeout: int = 3 * 3600) -> dict:
        deadline = time.time() + timeout
        while True:
            run = self._req("GET", f"/actor-runs/{run_id}", params={"waitForFinish": 60})["data"]
            if run["status"] in TERMINAL:
                return run
            if time.time() > deadline:
                raise ApifyError(f"Запуск {run_id} не завершился за {timeout} с")

    def usage(self) -> tuple[float, float]:
        """(потрачено в этом месяце, лимит в месяц) в долларах."""
        d = self._req("GET", "/users/me/limits")["data"]
        return (float((d.get("current") or {}).get("monthlyUsageUsd") or 0),
                float((d.get("limits") or {}).get("maxMonthlyUsageUsd") or 0))

    def dataset_items(self, dataset_id: str, page: int = 1000) -> list[dict]:
        items, offset = [], 0
        while True:
            chunk = self._req("GET", f"/datasets/{dataset_id}/items",
                              params={"clean": 1, "format": "json", "offset": offset, "limit": page})
            items += chunk
            if len(chunk) < page:
                return items
            offset += page
