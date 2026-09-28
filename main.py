"""Автопостинг роликов студии: Apify → уникализация → Postmypost, управление из Telegram.

  python main.py serve                                        бот + рабочий процесс (основной режим)
  python main.py status                                       что происходит
  python main.py tick                                         один круг рабочего процесса (для отладки)

  python main.py sources add <аккаунт|ссылка>... [--group ИМЯ]   основные аккаунты студии
  python main.py sources list | on <аккаунт> | off <аккаунт>
  python main.py ingest                                       забрать новые ролики сейчас
  python main.py posts [--status СТАТУС]                      собранные ролики

  python main.py accounts                                     аккаунты в Postmypost (как есть)
  python main.py dest sync                                    подтянуть сателлиты из Postmypost
  python main.py dest list
  python main.py dest set <id|all> [--group Г] [--slots 10:00,15:00,20:00] [--start ГГГГ-ММ-ДД] [--on|--off]

  python main.py set drafts on|off | review auto|cta|all | paused 0|1
"""
import argparse
import json
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler

from app import db, destinations, sources
from app import postmypost as pmp
from app.apify import Apify, ApifyError
from app.config import Settings, load
from app.ingest import Ingestor

log = logging.getLogger("main")


def need(value, name: str):
    if not value:
        sys.exit(f"Не задано {name} в .env")
    return value


def setup_logging(cfg: Settings) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file = RotatingFileHandler(cfg.data_dir / "app.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    console = logging.StreamHandler(sys.stdout)
    for h in (file, console):
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[file, console])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)


# --- основной режим ------------------------------------------------------------------

def run_bot(cfg: Settings) -> None:
    """Бот живёт, пока его не остановят: сетевой сбой при старте — пауза и новая попытка."""
    import time

    from telegram import Update
    from telegram.error import NetworkError

    from app.bot import build_app
    if not cfg.telegram_admin_ids:
        log.warning("TELEGRAM_ADMIN_IDS пуст — напиши боту /start, он пришлёт твой ID")
    while True:
        try:
            log.info("Telegram-бот запускается")
            # явно все типы событий: Telegram запоминает allowed_updates прошлого клиента,
            # и без этого нажатия inline-кнопок (callback_query) могут не приходить вовсе
            build_app(cfg).run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1,
                                       close_loop=False)
            return  # штатная остановка (Ctrl+C)
        except NetworkError as e:
            log.warning("Telegram недоступен (%s) — новая попытка через 15 с", e)
            time.sleep(15)


def cmd_serve(cfg: Settings) -> None:
    from app.worker import Worker

    setup_logging(cfg)
    stop = threading.Event()
    worker = Worker(cfg, echo_outbox=not cfg.telegram_bot_token)
    thread = threading.Thread(target=worker.run_forever, args=(stop,), name="worker", daemon=True)
    thread.start()
    try:
        if cfg.telegram_bot_token:
            run_bot(cfg)
        else:
            log.warning("TELEGRAM_BOT_TOKEN не задан — работаю без бота, уведомления пишу в лог")
            while thread.is_alive():
                thread.join(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        log.info("Останавливаюсь: дожидаюсь окончания текущего шага…")
        thread.join(timeout=600)


def cmd_status(cfg: Settings) -> None:
    from app.captions import Captioner
    from app.pipeline import Pipeline
    conn = db.connect(cfg.db_path)
    print(Pipeline(conn, cfg, captioner=Captioner(), render=lambda *_: {}).status_text())


def cmd_tick(cfg: Settings) -> None:
    from app.worker import Worker
    setup_logging(cfg)
    Worker(cfg, echo_outbox=True).tick()


# --- основные аккаунты и сбор роликов ----------------------------------------------------

def cmd_sources(cfg: Settings, a) -> None:
    conn = db.connect(cfg.db_path)
    if a.action == "add":
        for value in a.names:
            try:
                name, created = sources.add(conn, value, a.group)
            except ValueError as e:
                print(e)
                continue
            print(f"{'добавлен' if created else 'уже есть'}: {name} (группа {sources.group_of(conn, name)})")
    elif a.action in ("on", "off"):
        for value in a.names:
            ok = sources.set_enabled(conn, value, a.action == "on")
            print(f"{value}: {'готово' if ok else 'не найден'}")
    else:
        rows = sources.list_with_stats(conn)
        if not rows:
            print("Аккаунтов нет. Добавь: python main.py sources add <аккаунт>")
        for r in rows:
            state = "вкл " if r["enabled"] else "выкл"
            archive = "архив собран" if r["archive_done"] else "архив не собран"
            print(f"[{state}] {r['username']:<30} группа={r['route_group']:<12} "
                  f"роликов={r['posts'] or 0} скачано={r['downloaded'] or 0} "
                  f"ошибок={r['failed'] or 0}  {archive}, проверка: {r['last_scraped_at'] or '—'}")


def cmd_ingest(cfg: Settings) -> None:
    conn = db.connect(cfg.db_path)
    try:
        rep = Ingestor(conn, Apify(need(cfg.apify_token, "APIFY_TOKEN")), cfg).run()
    except ApifyError as e:
        sys.exit(f"Apify: {e}")
    print(f"Запусков Apify: {rep.runs}, получено: {rep.items}, новых: {rep.new}, "
          f"скачано: {rep.downloaded}, дублей: {rep.duplicates}, обновлено ссылок: {rep.refreshed}, "
          f"пропущено: {rep.skipped}, не скачалось: {rep.failed}")
    for err in rep.errors:
        print("  ошибка:", err)


def cmd_posts(cfg: Settings, a) -> None:
    conn = db.connect(cfg.db_path)
    for r in conn.execute("SELECT status, COUNT(*) n FROM source_posts GROUP BY status"):
        print(f"{r['status']:<14} {r['n']}")
    q = ("SELECT p.*, s.username FROM source_posts p JOIN sources s ON s.id = p.source_id "
         + ("WHERE p.status = ? " if a.status else "") + "ORDER BY p.published_at DESC LIMIT ?")
    rows = conn.execute(q, (a.status, a.limit) if a.status else (a.limit,)).fetchall()
    if rows:
        print()
    for r in rows:
        caption = (r["caption"] or "").replace("\n", " ")[:60]
        print(f"{(r['published_at'] or '')[:10]} {r['username']:<20} {r['shortcode']:<14} "
              f"{r['status']:<13} {caption}" + (f"  [{r['error']}]" if r["error"] else ""))


# --- Postmypost и сателлиты ----------------------------------------------------------------

def cmd_accounts(cfg: Settings) -> None:
    api = pmp.Postmypost(need(cfg.postmypost_token, "POSTMYPOST_TOKEN"))
    channels = {c["id"]: c.get("name", c.get("code")) for c in api.channels()}
    for p in api.projects():
        print(f"\nПроект {p['id']}: {p['name']}")
        for acc in api.accounts(p["id"]):
            ok = "ok" if acc.get("connection_status") == 1 else "НУЖНА АВТОРИЗАЦИЯ"
            print(f"  account_id={acc['id']:<8} {channels.get(acc.get('chanel_id'), '?'):<12} "
                  f"{acc.get('name', '')} [{ok}]")


def cmd_dest(cfg: Settings, a) -> None:
    conn = db.connect(cfg.db_path)
    if a.action == "sync":
        api = pmp.Postmypost(need(cfg.postmypost_token, "POSTMYPOST_TOKEN"))
        added, updated, warnings = destinations.sync_from_postmypost(
            conn, api, need(cfg.postmypost_project_id, "POSTMYPOST_PROJECT_ID"), cfg.default_slots)
        print(f"Новых: {added}, обновлено: {updated}")
        for w in warnings:
            print("  ", w)
        print("Новые аккаунты публикуют сразу. Отложить старт: dest set <id> --start ГГГГ-ММ-ДД, выключить: --off")
    elif a.action == "set":
        if not a.ref:
            sys.exit("Укажи id аккаунта (dest list) или all")
        try:
            ids = destinations.resolve(conn, a.ref)
            fields = {}
            if a.group:
                fields["route_group"] = a.group
            if a.slots:
                fields["slots"] = destinations.parse_slots(a.slots.split(","))
            if a.start:
                fields["start_date"] = destinations.parse_date(a.start)
            if a.on or a.off:
                fields["enabled"] = int(bool(a.on))
        except ValueError as e:
            sys.exit(str(e))
        if not fields:
            sys.exit("Нечего менять: --group, --slots, --start, --on/--off")
        destinations.update(conn, ids, **fields)
        for i in ids:
            db.add_command(conn, "unschedule", str(i))
        print(f"Готово для аккаунтов: {len(ids)}. Будущие публикации пересоберутся при следующем тике.")
    else:
        for r in destinations.all_rows(conn):
            print(f"[{r['id']}] {'вкл ' if r['enabled'] else 'выкл'} {r['name']:<25} {r['platform']:<10} "
                  f"группа={r['route_group']:<10} слоты={' '.join(json.loads(r['slots']))} "
                  f"старт={r['start_date'] or '—'}  (postmypost id {r['pmp_account_id']})")


def cmd_set(cfg: Settings, a) -> None:
    allowed = {"drafts": ("draft_mode", {"on": "1", "off": "0"}),
               "review": ("review_mode", {"auto": "auto", "cta": "cta", "all": "all"}),
               "paused": ("paused", {"0": "0", "1": "1"})}
    key, values = allowed[a.name]
    if a.value not in values:
        sys.exit(f"Допустимо: {', '.join(values)}")
    conn = db.connect(cfg.db_path)
    db.set_kv(conn, key, values[a.value])
    if a.name == "drafts" and a.value == "off":
        db.add_command(conn, "undraft")  # будущие черновики пересоздать настоящими
    print(f"{a.name} = {a.value}")


# --- разбор аргументов --------------------------------------------------------------

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = load()
    ap = argparse.ArgumentParser(description="Автопостинг роликов студии")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="бот + рабочий процесс")
    sub.add_parser("status", help="что происходит")
    sub.add_parser("tick", help="один круг рабочего процесса")

    s = sub.add_parser("sources", help="основные аккаунты студии")
    s.add_argument("action", choices=["add", "list", "on", "off"])
    s.add_argument("names", nargs="*")
    s.add_argument("--group", default="default", help="группа: её ролики идут в её сателлиты")

    sub.add_parser("ingest", help="забрать новые ролики через Apify")

    p = sub.add_parser("posts", help="собранные ролики")
    p.add_argument("--status")
    p.add_argument("--limit", type=int, default=20)

    sub.add_parser("accounts", help="аккаунты в Postmypost")

    d = sub.add_parser("dest", help="аккаунты-сателлиты")
    d.add_argument("action", choices=["sync", "list", "set"])
    d.add_argument("ref", nargs="?")
    d.add_argument("--group")
    d.add_argument("--slots", help="10:00,15:00,20:00")
    d.add_argument("--start", help="ГГГГ-ММ-ДД")
    d.add_argument("--on", action="store_true")
    d.add_argument("--off", action="store_true")

    st = sub.add_parser("set", help="режимы: drafts on|off, review auto|cta|all, paused 0|1")
    st.add_argument("name", choices=["drafts", "review", "paused"])
    st.add_argument("value")
    a = ap.parse_args()

    if a.cmd == "sources" and a.action in ("add", "on", "off") and not a.names:
        sys.exit("Укажи хотя бы один аккаунт")
    {"serve": lambda: cmd_serve(cfg), "status": lambda: cmd_status(cfg), "tick": lambda: cmd_tick(cfg),
     "sources": lambda: cmd_sources(cfg, a), "ingest": lambda: cmd_ingest(cfg),
     "posts": lambda: cmd_posts(cfg, a), "accounts": lambda: cmd_accounts(cfg),
     "dest": lambda: cmd_dest(cfg, a), "set": lambda: cmd_set(cfg, a)}[a.cmd]()


if __name__ == "__main__":
    main()
