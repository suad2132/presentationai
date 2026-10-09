import json
import os
import re
import shutil
import subprocess
import threading
import traceback
import uuid
import webbrowser
from pathlib import Path

import requests
from flask import Flask, request, jsonify, send_from_directory

from builder import build_presentation, clean_content, clean_text

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # не принимаем запросы больше 2 МБ

MIN_SLIDES, MAX_SLIDES = 2, 30
MAX_CONTENT_CHARS = 30000


def env(name, default=""):
    """Читает настройку и убирает случайные пробелы и кавычки."""
    return os.environ.get(name, default).strip().strip('"').strip("'").strip()


# ---------- НАСТРОЙКИ (берутся из окружения, задаются в start.bat) ----------
API_KEY = env("LLM_API_KEY")
MODEL = env("LLM_MODEL")
BASE_URL = env("LLM_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
if BASE_URL.endswith("/chat/completions"):
    BASE_URL = BASE_URL[: -len("/chat/completions")]
try:
    PORT = int(env("PORT", "5000"))
except ValueError:
    PORT = 5000

SYSTEM_PROMPT = """Ты помощник, который готовит структуру учебной презентации для вуза.
Тебе дают тему, исходный материал и нужное число слайдов.
Верни СТРОГО JSON, без пояснений и без markdown, в таком виде:
{"slides": [{"title": "Заголовок", "bullets": ["пункт 1", "пункт 2", "пункт 3"]}]}
Правила:
- ровно столько слайдов, сколько просили;
- первый слайд титульный: title = тема, bullets пустой список;
- на остальных слайдах 3-5 коротких пунктов (до 15 слов каждый);
- опирайся на материал пользователя, ничего не выдумывай;
- пиши на языке материала."""


class UserError(Exception):
    """Ошибка, текст которой можно показать пользователю на странице."""


# ---------- ИИ ----------
def ai_configured():
    return bool(API_KEY and MODEL)


def provider_message(response):
    """Достаёт текст ошибки из ответа сервиса (если он есть)."""
    try:
        err = response.json().get("error")
        text = err.get("message") if isinstance(err, dict) else err
        return clean_text(text or "")[:200]
    except Exception:
        return ""


def ask_llm(topic, content, slides):
    """Отправляет запрос модели и возвращает «сырой» ответ."""
    if not ai_configured():
        raise UserError("ИИ не настроен: впишите ключ и модель в начале файла start.bat "
                        "или снимите галочку «Подключить ИИ».")

    user_text = f"Тема: {topic}\nЧисло слайдов: {slides}\n\nМатериал:\n{content}"
    try:
        response = requests.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
            },
            timeout=120,
        )
    except requests.exceptions.Timeout:
        raise UserError("Модель слишком долго не отвечает. Попробуйте ещё раз или сократите текст.")
    except requests.exceptions.RequestException:
        raise UserError("Нет связи с сервисом ИИ. Проверьте интернет и адрес LLM_BASE_URL.")

    code = response.status_code
    if code in (401, 403):
        raise UserError(f"Сервис не принял ключ (ошибка {code}). Проверьте LLM_API_KEY.")
    if code == 402:
        raise UserError("На счёте сервиса не хватает средств (ошибка 402). Пополните баланс.")
    if code == 404:
        raise UserError("Не найдены модель или адрес сервиса (ошибка 404). Проверьте LLM_MODEL и LLM_BASE_URL.")
    if code == 429:
        raise UserError("Слишком много запросов (ошибка 429). Подождите минуту и повторите.")
    if code >= 400:
        details = provider_message(response)
        raise UserError(f"Сервис ИИ вернул ошибку {code}. {details}".strip())

    try:
        answer = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise UserError("Сервис ИИ вернул неожиданный ответ. Проверьте LLM_MODEL и LLM_BASE_URL.")
    if not answer or not str(answer).strip():
        raise UserError("Модель вернула пустой ответ. Попробуйте ещё раз или выберите другую модель.")
    return str(answer)


def parse_llm_answer(answer):
    """Достаёт JSON из ответа модели (убирает ```json, блоки <think> и лишние слова вокруг)."""
    answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.DOTALL)
    answer = answer.replace("```json", "").replace("```", "").strip()
    # Сначала ищем объект {...}, потом список [...]
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start, end = answer.find(open_ch), answer.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(answer[start:end + 1])
            except ValueError:
                continue
    raise UserError(FORMAT_ERROR)


FORMAT_ERROR = ("Модель ответила не в нужном формате. Нажмите кнопку ещё раз, выберите другую модель "
                "или снимите галочку «Подключить ИИ».")


def normalize_outline(raw, topic, slides):
    """Приводит ответ модели к чистому списку слайдов."""
    items = raw.get("slides") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise UserError(FORMAT_ERROR)

    outline = []
    for item in items:
        if isinstance(item, str):
            item = {"title": item, "bullets": []}
        if not isinstance(item, dict):
            continue
        bullets = item.get("bullets") or item.get("points") or []
        if isinstance(bullets, str):
            bullets = [bullets]
        if not isinstance(bullets, list):
            bullets = []
        bullets = [clean_text(b) for b in bullets if isinstance(b, (str, int, float)) and clean_text(b)]
        title = clean_text(item.get("title") or "")
        if not title and not bullets:
            continue
        outline.append({"title": title or topic, "bullets": bullets})

    if not outline:
        raise UserError(FORMAT_ERROR)
    # Первый слайд должен быть титульным, без пунктов
    if outline[0]["bullets"]:
        outline.insert(0, {"title": topic, "bullets": []})
    return outline[:slides]


# ---------- Режим без ИИ ----------
def slides_word(n):
    """Правильное склонение: 1 слайд, 2 слайда, 5 слайдов."""
    if n % 100 in range(11, 15):
        return "слайдов"
    return {1: "слайд", 2: "слайда", 3: "слайда", 4: "слайда"}.get(n % 10, "слайдов")


def split_without_ai(topic, content, slides):
    """Делит текст на слайды по абзацам (а если абзацев мало, то по предложениям)."""
    body_count = max(slides - 1, 1)
    paragraphs = [p for p in (clean_text(x) for x in content.split("\n")) if p]
    if len(paragraphs) < body_count:
        sentences = []
        for p in paragraphs:
            sentences += [s for s in re.split(r"(?<=[.!?…])\s+", p) if s]
        paragraphs = sentences or paragraphs

    # Раскладываем абзацы по слайдам как можно ровнее
    groups = min(body_count, len(paragraphs))
    base, extra = divmod(len(paragraphs), groups)
    result = [{"title": topic, "bullets": []}]
    position = 0
    for g in range(groups):
        count = base + (1 if g < extra else 0)
        chunk = paragraphs[position:position + count]
        position += count
        first = chunk[0]
        if len(first) > 70:
            title = first[:70].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
            bullets = chunk
        else:
            title, bullets = first, chunk[1:]
        result.append({"title": title, "bullets": bullets})
    return result[:slides]


# ---------- PDF ----------
PDF_LOCK = threading.Lock()


def find_soffice():
    """Ищет LibreOffice на компьютере. Если не нашли, вернёт None."""
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    candidates = [
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def convert_to_pdf(pptx_path):
    """Превращает .pptx в PDF через LibreOffice. Возвращает имя PDF или None."""
    soffice = find_soffice()
    if not soffice:
        return None
    # Отдельный профиль, чтобы не мешал LibreOffice, уже открытый у вас на экране
    profile = (BASE_DIR / ".lo_profile").resolve().as_uri()
    cmd = [soffice, f"-env:UserInstallation={profile}", "--headless",
           "--convert-to", "pdf", "--outdir", str(OUTPUT_DIR), str(pptx_path)]
    try:
        with PDF_LOCK:
            subprocess.run(cmd, timeout=180, check=True, capture_output=True)
    except Exception as err:
        print("PDF не создан:", err)
        return None
    pdf_name = Path(pptx_path).with_suffix(".pdf").name
    return pdf_name if (OUTPUT_DIR / pdf_name).exists() else None


# ---------- Страницы ----------
@app.route("/")
def home():
    response = send_from_directory(BASE_DIR, "index.html")
    response.headers["Cache-Control"] = "no-store"  # чтобы браузер не показывал старую версию
    return response


@app.route("/create", methods=["POST"])
def create():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"message": "Не удалось прочитать данные формы."}), 400

    topic = clean_text(data.get("topic") or "")[:200]
    content = clean_content(data.get("content") or "")
    try:
        slides = int(data.get("slides", 10))
    except (TypeError, ValueError):
        slides = 10
    slides = max(MIN_SLIDES, min(MAX_SLIDES, slides))
    use_ai = data.get("useAI") is True

    if not topic or not content:
        return jsonify({"message": "Заполните тему и содержание."}), 400
    if len(content) > MAX_CONTENT_CHARS:
        return jsonify({"message": f"Текст слишком длинный: максимум {MAX_CONTENT_CHARS} символов."}), 400

    try:
        if use_ai:
            outline = normalize_outline(parse_llm_answer(ask_llm(topic, content, slides)), topic, slides)
        else:
            outline = split_without_ai(topic, content, slides)

        file_name = f"presentation_{uuid.uuid4().hex[:8]}.pptx"
        pptx_path = OUTPUT_DIR / file_name
        build_presentation(topic, outline, pptx_path)
    except UserError as err:
        return jsonify({"message": str(err)}), 400
    except Exception as err:
        traceback.print_exc()
        return jsonify({"message": f"Внутренняя ошибка: {err}. Подробности в окне с сервером."}), 500

    pdf_name = convert_to_pdf(pptx_path)
    return jsonify({
        "message": f"Презентация готова: {len(outline)} {slides_word(len(outline))}.",
        "slides": outline,
        "download_url": f"/download/{file_name}",
        "pdf_url": f"/download/{pdf_name}" if pdf_name else None,
    })


@app.route("/download/<name>")
def download(name):
    if not re.fullmatch(r"presentation_[0-9a-f]{8}\.(pptx|pdf)", name):
        return "Файл не найден", 404
    return send_from_directory(OUTPUT_DIR, name, as_attachment=True)


@app.errorhandler(413)
def too_large(_):
    return jsonify({"message": "Слишком большой запрос. Сократите текст."}), 413


if __name__ == "__main__":
    url = f"http://127.0.0.1:{PORT}"
    print("Сайт запущен:", url)
    if ai_configured():
        print("ИИ подключён, модель:", MODEL)
    else:
        print("ИИ не настроен: работает только режим без ИИ.")
    if not find_soffice():
        print("LibreOffice не найден: PDF создаваться не будет.")
    if os.environ.get("OPEN_BROWSER") == "1":
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, debug=False)
