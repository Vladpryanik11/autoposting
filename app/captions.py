"""Подписи: очистка, поиск кодового слова (CTA), лёгкий рерайт через Claude.

Гарантии:
- в итоговом тексте нет @упоминаний и ссылок;
- найденное кодовое слово сохраняется дословно — если ИИ его потерял, берём очищенный оригинал;
- если призыв к действию есть, а кодовое слово не распознано — публикация уходит на проверку.
"""
import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

URL_RE = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:com|ru|io|me|link|ly|cc|org|net|site|shop|pro|app|to)"
                    r"(?:/\S*)?\b", re.I)
MENTION_RE = re.compile(r"(?<![\w@.])@[\w.]{1,30}\w")
HASHTAG_RE = re.compile(r"#\w+")
FOLLOW_RE = re.compile(r"подпис|follow|переход|заход|ссылк|link|профил|profile|шапк|bio|подробн|"
                       r"more|сайт|site", re.I)

TRIGGER_RE = re.compile(r"\b(?:напиш|пиш|оставь|оставляй|отправ|кин|скин|пришл|ставь|жми|комментир|"
                        r"comment|type|write|send|drop|dm\b)\w*", re.I)
CHANNEL_RE = re.compile(r"коммент|директ|direct|\bdm\b|\bлс\b|личк|сообщени|comment|below", re.I)
PROMISE_RE = re.compile(r"пришл|отправл|скин|вышл|получ|дам|гайд|инструкц|чек.?лист|шаблон|ссылк|доступ|"
                        r"бесплатн|подар|урок|файл|send you|get|guide|free|link", re.I)
QUOTED_RE = re.compile(r"[«\"“„'‘]([^«»\"“”„'‘’\n]{1,30})[»\"”“'’]")
WORD_AFTER_RE = re.compile(r"\b(?:слово|word)\s+[«\"“„']?([\wЁё+\-]{1,30})", re.I)
CAPS_RE = re.compile(r"\b([A-ZА-ЯЁ][A-ZА-ЯЁ0-9+\-]{1,19})\b")
NOT_KEYWORDS = {"DM", "ЛС", "ИИ", "AI", "IG", "TT", "YT", "PS", "OK", "ОК", "USA", "РФ"}
STOPWORDS = {"и", "в", "во", "на", "по", "из", "для", "с", "со", "к", "а", "я", "мне", "тебе", "вам", "ниже",
             "and", "in", "to", "below", "the", "a", "me", "you"}

NEXT_WORD_RE = re.compile(r"[ \t]+([A-Za-zА-Яа-яЁё0-9+\-]{2,20})\b")
NOT_NEXT_WORDS = {"мне", "нам", "что", "как", "свой", "своё", "свое", "свои", "это", "сюда", "если", "когда",
                  "под", "пожалуйста", "ваше", "твоё", "твое", "своих", "отзыв", "вопрос", "вопросы",
                  "комментарий", "комментарии", "комментариях", "коммент", "комменты", "директ", "личку",
                  "сообщение", "сообщения", "нашем", "нашем", "мнение", "свою", "any", "your", "us", "me"}

PLATFORM_LIMITS = {  # (макс. длина текста, макс. хэштегов)
    "instagram": (2200, 30),
    "tiktok": (2200, 30),
    "youtube": (5000, 15),
}
DEFAULT_LIMITS = (2200, 30)


@dataclass(frozen=True)
class Cta:
    state: str                 # none | keyword | uncertain
    keyword: str | None = None


@dataclass(frozen=True)
class CaptionResult:
    text: str
    title: str
    review_reason: str | None = None
    rewritten: bool = False


def clean(text: str | None) -> str:
    """Убирает ссылки и @упоминания; строки «подпишись на @…» удаляет целиком."""
    lines = []
    for line in (text or "").splitlines():
        had_ref = bool(MENTION_RE.search(line) or URL_RE.search(line))
        if had_ref and FOLLOW_RE.search(line):
            continue
        line = URL_RE.sub("", MENTION_RE.sub("", line))
        line = re.sub(r"[ \t]{2,}", " ", line).strip()
        if had_ref and not re.search(r"\w", line):
            continue  # от строки остались только знаки/эмодзи
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def extract_cta(text: str | None) -> Cta:
    text = text or ""
    for m in TRIGGER_RE.finditer(text):
        window = text[m.end(): m.end() + 70].split("\n")[0]
        k = QUOTED_RE.search(window)
        if k and k.group(1).strip():
            return Cta("keyword", k.group(1).strip())
        k = WORD_AFTER_RE.search(window)
        if k and len(k.group(1)) > 1 and k.group(1).lower() not in STOPWORDS:
            return Cta("keyword", k.group(1))
        caps = [c for c in CAPS_RE.findall(window) if c not in NOT_KEYWORDS]
        if caps:
            return Cta("keyword", caps[0])
    # «ГАЙД» в комментарии — без глагола, но рядом с каналом
    for k in QUOTED_RE.finditer(text):
        near = text[max(0, k.start() - 40): k.end() + 40]
        if CHANNEL_RE.search(near):
            return Cta("keyword", k.group(1).strip())
    # «пиши хочу. Скину промт», «Пиши урок в комментарии» — обычное слово сразу после глагола,
    # если рядом есть канал (комментарии/директ) или обещание (пришлю/скину/инструкция)
    for m in TRIGGER_RE.finditer(text):
        k = NEXT_WORD_RE.match(text, m.end())
        if not k or k.group(1).lower() in STOPWORDS | NOT_NEXT_WORDS or CHANNEL_RE.fullmatch(k.group(1)):
            continue
        sentence = text[max(0, m.start() - 60): m.end() + 120]
        if CHANNEL_RE.search(sentence) or PROMISE_RE.search(sentence):
            return Cta("keyword", k.group(1))
    for m in TRIGGER_RE.finditer(text):
        sentence = text[max(0, m.start() - 80): m.end() + 120]
        if CHANNEL_RE.search(sentence) and PROMISE_RE.search(sentence):
            return Cta("uncertain")
    return Cta("none")


def has_keyword(text: str, keyword: str) -> bool:
    if re.fullmatch(r"\w+", keyword):
        return re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", text, re.I) is not None
    return keyword.casefold() in text.casefold()


def limit_hashtags(text: str, max_tags: int) -> str:
    tags = HASHTAG_RE.findall(text)
    for tag in tags[max_tags:]:
        text = re.sub(rf"\s*{re.escape(tag)}(?!\w)", "", text, count=1)
    return text.strip()


def make_title(text: str, fallback: str = "Новое видео") -> str:
    """Заголовок YouTube: первая содержательная строка без хэштегов, ≤100 символов."""
    for line in text.splitlines():
        line = HASHTAG_RE.sub("", line).replace("<", "").replace(">", "").strip(" -–—•|")
        if re.search(r"\w", line):
            if len(line) <= 100:
                return line
            cut = line[:100].rsplit(" ", 1)[0]
            return (cut if len(cut) > 40 else line[:100]).rstrip(" ,.;:—-")
    return fallback


SYSTEM_PROMPT = """Ты редактор подписей к коротким вертикальным видео (Reels, TikTok, Shorts).
Перепиши подпись своими словами, чтобы она не совпадала дословно с исходной, но:
- смысл, язык, тон, обращение (ты/вы) и эмодзи-стиль остаются прежними;
- длина примерно та же (±20%), разбивка на абзацы похожая;
- обещания не меняются: не добавляй того, чего не было в исходнике;
- хэштеги оставь (можно поменять порядок и убрать до трети), новых не придумывай;
- никаких @упоминаний, ссылок и адресов сайтов;
- если задано кодовое слово — оно обязано остаться ровно в том же написании и регистре, в кавычках «».
Ответь только готовым текстом подписи, без пояснений и вступлений."""


class Captioner:
    def __init__(self, api_key: str = "", model: str = "claude-haiku-4-5", client=None):
        self.model = model
        self.client = client
        if self.client is None and api_key:
            import anthropic
            self.client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=60.0)

    def make(self, original: str | None, platform: str, review_mode: str = "auto") -> CaptionResult:
        cta = extract_cta(original)
        base = clean(original)
        max_len, max_tags = PLATFORM_LIMITS.get(platform, DEFAULT_LIMITS)
        text, rewritten = base, False
        if self.client and base:
            candidate = self._rewrite(base, cta.keyword, max_len)
            if candidate:
                text, rewritten = candidate, True
        text = limit_hashtags(text, max_tags)[:max_len]

        reason = None
        if cta.state == "uncertain":
            reason = "есть призыв к действию, но кодовое слово не распознано"
        elif review_mode == "cta" and cta.keyword:
            reason = f"кодовое слово «{cta.keyword}»"
        elif review_mode == "all":
            reason = "проверка всех публикаций включена"
        return CaptionResult(text=text, title=make_title(text), review_reason=reason, rewritten=rewritten)

    def _rewrite(self, base: str, keyword: str | None, max_len: int) -> str | None:
        import anthropic

        prompt = f"Исходная подпись:\n<<<\n{base}\n>>>"
        if keyword:
            prompt += f"\n\nКодовое слово: «{keyword}»"
        for attempt in range(2):
            try:
                resp = self.client.messages.create(
                    model=self.model, max_tokens=2048, temperature=1.0,
                    system=SYSTEM_PROMPT, messages=[{"role": "user", "content": prompt}])
            except (anthropic.APIConnectionError, anthropic.APIStatusError) as e:
                log.warning("Claude недоступен, оставляю очищенный оригинал: %s", e)
                return None
            if resp.stop_reason not in ("end_turn", "stop_sequence"):
                log.warning("Claude: stop_reason=%s, попытка %d", resp.stop_reason, attempt + 1)
                continue
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            text = re.sub(r"^(?:вот|here is|here's)[^\n]*:\s*\n", "", text, flags=re.I).strip()
            problem = self._check(text, keyword, max_len)
            if not problem:
                return text
            log.warning("Рерайт отклонён (%s), попытка %d", problem, attempt + 1)
        return None

    @staticmethod
    def _check(text: str, keyword: str | None, max_len: int) -> str | None:
        if not text:
            return "пустой ответ"
        if keyword and not has_keyword(text, keyword):
            return f"потеряно кодовое слово «{keyword}»"
        if MENTION_RE.search(text) or URL_RE.search(text):
            return "появились упоминание или ссылка"
        if len(text) > max_len:
            return "слишком длинно"
        return None
