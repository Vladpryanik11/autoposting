from types import SimpleNamespace

import anthropic
import pytest

from app.captions import Captioner, clean, extract_cta, has_keyword, limit_hashtags, make_title


@pytest.mark.parametrize("text, keyword", [
    ("5 нейросетей.\nНапиши «Промт» в комментарии и я пришлю тебе инструкцию.", "Промт"),
    ("Пиши ПРОМТ в директ — скину гайд", "ПРОМТ"),
    ('Comment "GUIDE" below and I will send you the link', "GUIDE"),
    ("Напиши слово гайд в комментарии", "гайд"),
    ("Слово «ПЛАН» в комменты — пришлю чек-лист", "ПЛАН"),
    ("Оставь «+» в комментариях, отправлю шаблон", "+"),
])
def test_cta_keyword_found(text, keyword):
    assert extract_cta(text).keyword == keyword


@pytest.mark.parametrize("text", [
    "Пишите в комментариях, что думаете",
    "Как ИИ меняет работу дизайнера. #ии #дизайн",
    "",
    None,
])
def test_no_cta(text):
    assert extract_cta(text).state == "none"


def test_cta_without_recognizable_keyword_is_uncertain():
    assert extract_cta("Напиши в комментарии кодовое слово и я пришлю тебе гайд").state == "uncertain"


def test_clean_removes_links_mentions_and_follow_lines():
    text = ("Лучшие приёмы монтажа 🔥\n"
            "Подписывайся на @studio.main 👇\n"
            "Подробнее на https://site.ru/page и studio.com\n"
            "Снято с @operator_1 на iPhone\n"
            "#монтаж #reels")
    out = clean(text)
    assert "@" not in out and "http" not in out and "studio.com" not in out
    assert "Подписывайся" not in out and "Подробнее" not in out  # строки-переходы удалены целиком
    assert "Снято с на iPhone" in out and "#монтаж #reels" in out
    assert out.startswith("Лучшие приёмы монтажа 🔥")


def test_clean_keeps_emails_free_text_intact():
    assert clean("Обычный текст без ссылок.\n\nВторой абзац") == "Обычный текст без ссылок.\n\nВторой абзац"


def test_has_keyword_whole_word_and_symbols():
    assert has_keyword("Пиши «ПРОМТ» ниже", "Промт")
    assert not has_keyword("промтинг — это", "промт")
    assert has_keyword("Ставь «+»", "+")


def test_limit_hashtags():
    text = "Текст\n" + " ".join(f"#тег{i}" for i in range(40))
    out = limit_hashtags(text, 30)
    assert out.count("#") == 30 and "#тег29" in out and "#тег30" not in out


def test_make_title():
    assert make_title("#reels\n5 нейросетей для работы 🔥 #ии\nтекст") == "5 нейросетей для работы 🔥"
    long = "Слово " * 40
    t = make_title(long)
    assert len(t) <= 100 and not t.endswith(" ")
    assert make_title("#только #теги") == "Новое видео"


# --- рерайт -----------------------------------------------------------------------

class FakeClaude:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=a)])


ORIGINAL = "5 нейросетей.\nНапиши «Промт» в комментарии — пришлю инструкцию.\n#ии #нейросети"


def test_without_api_key_uses_cleaned_original():
    r = Captioner().make("Текст @studio\n#теги", "instagram")
    assert (r.text, r.rewritten, r.review_reason) == ("Текст\n#теги", False, None)


def test_rewrite_keeps_keyword():
    fake = FakeClaude("Пять ИИ-инструментов.\nХочешь инструкцию? Пиши «Промт» в комментариях.\n#нейросети #ии")
    r = Captioner(client=fake).make(ORIGINAL, "instagram")
    assert r.rewritten and "«Промт»" in r.text
    assert "Кодовое слово: «Промт»" in fake.calls[0]["messages"][0]["content"]
    assert fake.calls[0]["model"] == "claude-haiku-4-5"


def test_rewrite_that_loses_keyword_falls_back_to_original():
    fake = FakeClaude("Пиши «ГАЙД» в комментах", "Пиши в комментах")
    r = Captioner(client=fake).make(ORIGINAL, "tiktok")
    assert not r.rewritten and "«Промт»" in r.text and len(fake.calls) == 2


def test_rewrite_with_link_is_rejected():
    fake = FakeClaude("Пиши «Промт» и заходи на https://spam.ru", "Пиши «Промт» в комментариях")
    r = Captioner(client=fake).make(ORIGINAL, "instagram")
    assert r.rewritten and "http" not in r.text


def test_rewrite_strips_preamble():
    fake = FakeClaude("Вот переписанная подпись:\nПиши «Промт» — пришлю")
    assert Captioner(client=fake).make(ORIGINAL, "instagram").text == "Пиши «Промт» — пришлю"


def test_api_error_falls_back_without_crashing():
    import httpx2
    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))
    r = Captioner(client=FakeClaude(err)).make(ORIGINAL, "instagram")
    assert not r.rewritten and "«Промт»" in r.text


@pytest.mark.parametrize("mode, text, expect_review", [
    ("auto", ORIGINAL, False),
    ("cta", ORIGINAL, True),
    ("cta", "Просто видео #reels", False),
    ("all", "Просто видео #reels", True),
    ("auto", "Напиши в комментарии и я пришлю тебе гайд", True),
])
def test_review_modes(mode, text, expect_review):
    assert (Captioner().make(text, "instagram", mode).review_reason is not None) == expect_review


def test_youtube_limits_hashtags_and_has_title():
    text = "Заголовок ролика\n" + " ".join(f"#t{i}" for i in range(25))
    r = Captioner().make(text, "youtube")
    assert r.text.count("#") == 15 and r.title == "Заголовок ролика"


@pytest.mark.parametrize("text, keyword", [
    # реальные подписи promtlaba (из аналитики Postmypost, 27.09)
    ("Если хочешь такой же, пиши хочу. Скину промт", "хочу"),
    ("Пиши урок в комментарии, пришлю подробную инструкцию", "урок"),
    ("Пиши в комментарии ПРОМТ. Вышлю инструкцию", "ПРОМТ"),
])
def test_cta_keyword_in_real_studio_captions(text, keyword):
    assert extract_cta(text).keyword == keyword


@pytest.mark.parametrize("text", [
    "Пишите в комментариях, что думаете",
    "Напиши мне в директ, если есть вопросы",
    "Пиши свой вариант в комментариях",
])
def test_plain_engagement_is_not_keyword(text):
    assert extract_cta(text).keyword is None
