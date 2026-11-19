"""Preview a native Telegram rich message or publish it to @GoTodayGo."""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import secrets
import subprocess
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from archive import text_digest
from rich_upload import rich_upload


CHANNEL = "@GoTodayGo"
CATALOG = Path(__file__).resolve().with_name("cards.json")
STATE = "publication-state.json"
# Slot zero is 01:18 UTC. These offsets correspond to 16:18, 19:18 and 22:18 in Moscow.
EVENING_OFFSETS = (4, 5, 6)


def slot(when):
    # The UTC schedule starts its first three-hour period at 01:18.
    return (int(when.timestamp()) - (3600 + 18 * 60)) // (3 * 3600)


def render(post):
    fields = ("title", "text", "photo", "source_url")
    if not isinstance(post, dict) or any(
        not isinstance(post.get(key), str) or not post[key].strip() for key in fields
    ):
        raise ValueError("Для карточки нужны заголовок, текст, файл изображения и ссылка на источник.")
    if any(not any(c.isalnum() for c in post[k]) for k in ("title", "text")):
        raise ValueError("Заголовок и текст должны содержать слова или цифры.")
    source = urlsplit(post["source_url"].strip())
    if source.scheme != "https" or not source.hostname or source.username is not None:
        raise ValueError("Для источника нужна ссылка HTTPS.")
    author = post.get("author", "")
    if not isinstance(author, str):
        raise ValueError("Имя автора должно быть строкой.")
    if not isinstance(post.get("theme", ""), str):
        raise ValueError("Тема карточки должна быть строкой.")
    author = author.strip()
    blocks = [
        {"type": "photo", "photo": {"type": "photo", "media": "attach://photo"}},
        {"type": "heading", "size": 1, "text": post["title"].strip()},
        {"type": "paragraph", "text": post["text"].strip()},
    ]
    if author:
        author_link = {"type": "url", "text": author, "url": post["source_url"].strip()}
        blocks.append({"type": "paragraph", "text": ["©️ ", author_link]})
    channel_link = {"type": "url", "text": "Сегодня тот самый день", "url": "https://t.me/GoTodayGo"}
    blocks.append({"type": "footer", "text": ["🌏 ", channel_link]})
    return {"blocks": blocks}


def photo_file(post):
    relative = Path(post["photo"])
    photo = (CATALOG.parent / relative).resolve()
    if relative.is_absolute() or not photo.is_relative_to(CATALOG.parent):
        raise ValueError("Изображение должно находиться в каталоге проекта.")
    if not photo.is_file() or not 0 < photo.stat().st_size <= 10_000_000:
        raise ValueError(f"Проверьте файл изображения и его размер: {relative}")
    with photo.open("rb") as stream:
        head = stream.read(8)
    if head.startswith(b"\xff\xd8\xff") and photo.suffix.lower() in {".jpg", ".jpeg"}:
        return photo, "image/jpeg"
    if head == b"\x89PNG\r\n\x1a\n" and photo.suffix.lower() == ".png":
        return photo, "image/png"
    raise ValueError(f"Ожидается фотография JPEG или PNG: {relative}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def bot_token():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ValueError("Проверьте секрет TELEGRAM_BOT_TOKEN.")
    return token


class UnverifiedMessage(RuntimeError):
    def __init__(self, receipt, reason):
        super().__init__(f"Сообщение {receipt['message_id']} в канале. {reason}")
        self.receipt = receipt


def send(rich_message, photo, media_type, *, edit_id=None):
    from photo_receipt import verify_photo

    token = bot_token()
    boundary = secrets.token_hex(24)
    fields = {"chat_id": CHANNEL, "rich_message": json.dumps(rich_upload(rich_message), ensure_ascii=False)}
    method = "sendRichMessage"
    filename = f"card{photo.suffix.lower()}"
    if edit_id is not None:
        from PIL import Image, ImageOps

        fields["message_id"] = str(edit_id)
        method = "editMessageText"
        # Telegram treats a byte-identical upload as a no-op when editing the same post.
        with Image.open(photo) as source:
            upload = ImageOps.exif_transpose(source).convert("RGB")
            longest = max(upload.size)
            target = min(1200, longest - 1) if longest > 1 else 1
            upload.thumbnail((target, target), Image.Resampling.LANCZOS)
            encoded = io.BytesIO()
            upload.save(encoded, "JPEG", quality=94, optimize=True)
            binary = encoded.getvalue()
        media_type, filename = "image/jpeg", "illustration.jpg"
    else:
        binary = photo.read_bytes()
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.extend([
        f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; '
        f'filename="{filename}"\r\nContent-Type: {media_type}\r\n\r\n'.encode(),
        binary,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=25) as response:
            reply = json.load(response)
    except HTTPError as exc:
        try:
            description = json.load(exc).get("description", "")
        except (ValueError, AttributeError):
            description = ""
        if isinstance(description, str):
            for detail in ("message is not modified", "message can't be edited", "message to edit not found",
                           "not enough rights", "can't parse", "wrong file identifier"):
                if detail in description.lower():
                    raise RuntimeError(f"Telegram {method}: {detail} (HTTP {exc.code}).") from None
        raise RuntimeError(f"Telegram вернул HTTP {exc.code}.") from None
    except (URLError, OSError, HTTPException, ValueError):
        raise RuntimeError("Не удалось подтвердить отправку. Проверьте историю канала.") from None
    result = reply.get("result") if isinstance(reply, dict) and reply.get("ok") is True else None
    if isinstance(result, dict):
        chat = result.get("chat", {})
        received = result.get("rich_message")
        blocks = received.get("blocks") if isinstance(received, dict) else None
        if (
            isinstance(chat, dict)
            and chat.get("type") == "channel"
            and str(chat.get("username", "")).casefold() == CHANNEL[1:].casefold()
            and type(result.get("message_id")) is int
            and result["message_id"] > 0
            and (edit_id is None or result["message_id"] == edit_id)
            and type(result.get("date")) is int
            and result["date"] > 0
        ):
            try:
                published = datetime.fromtimestamp(result["date"], timezone.utc)
            except (ValueError, OSError, OverflowError):
                raise RuntimeError("Telegram вернул некорректное время публикации.") from None
            receipt = {
                "message_id": result["message_id"],
                "published_at": published.isoformat().replace("+00:00", "Z"),
            }
            if (not isinstance(blocks, list) or not all(isinstance(b, dict) for b in blocks)
                    or [b.get("type") for b in blocks] != [b["type"] for b in rich_message["blocks"]]
                    or blocks[1].get("size") != 1):
                raise UnverifiedMessage(receipt, "Telegram не подтвердил формат карточки.")
            try:
                width, height = verify_photo(blocks, photo, token)
            except RuntimeError as error:
                raise UnverifiedMessage(receipt, str(error)) from None
            receipt.update(photo_width=width, photo_height=height)
            print(f"Фото из Telegram сверено с оригиналом: {width}×{height}. Сообщение {result['message_id']}.")
            return receipt
    raise RuntimeError("Telegram не подтвердил публикацию в ожидаемом канале.")



class StateConflict(Exception):
    """Another writer updated the state file after we read it."""


def github(endpoint, data=None):
    args = ["gh", "api", endpoint]
    if data is not None:
        args.extend(["--method", "PUT", "--input", "-"])
    response = subprocess.run(
        args, input=json.dumps(data) if data is not None else None,
        capture_output=True, text=True, timeout=45, check=False,
    )
    if response.returncode:
        if data is not None and "(HTTP 409)" in response.stderr:
            raise StateConflict()
        raise RuntimeError("GitHub не подтвердил операцию с журналом публикаций.")
    return json.loads(response.stdout)


class RepositoryState:
    def __init__(self):
        self.repo = os.environ["GITHUB_REPOSITORY"]
        self.branch = os.environ["GITHUB_REF_NAME"]
        self.endpoint = f"repos/{self.repo}/contents/{STATE}"

    def read(self):
        file = github(f"{self.endpoint}?ref={quote(self.branch, safe='')}")
        if file["encoding"] != "base64":
            # The Contents API omits inline content for files larger than 1 MiB.
            file = github(f"repos/{self.repo}/git/blobs/{file['sha']}")
        return validate_state(json.loads(base64.b64decode(file["content"]))), file["sha"]

    def write(self, state, sha, comment):
        content = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
        result = github(self.endpoint, {
            "message": comment, "sha": sha, "branch": self.branch,
            "content": base64.b64encode(content.encode()).decode(),
        })
        saved = result.get("content") or {}
        commit = result.get("commit") or {}
        if saved.get("path") != STATE or not saved.get("sha") or not commit.get("sha"):
            raise RuntimeError("GitHub не подтвердил фиксацию журнала публикаций.")


def validate_state(state):
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("entries"), list):
        raise ValueError("Журнал публикаций имеет неизвестный формат.")
    for entry in state["entries"]:
        if (
            not isinstance(entry, dict)
            or entry.get("status") not in {"reserved", "sent", "uncertain", "expired"}
            or not all(isinstance(entry.get(k), str) and entry[k] for k in ("title_key", "text_key"))
            or not re.fullmatch(r"[a-f0-9]{64}", str(entry.get("photo_sha256", "")))
            or "slot" not in entry
            or (entry["slot"] is not None and type(entry["slot"]) is not int)
        ):
            raise ValueError("В журнале публикаций есть неполная запись.")
    return state


def archive_keys():
    hashes = json.loads(CATALOG.with_name("archive_texts.json").read_text(encoding="utf-8"))
    if not isinstance(hashes, list) or not hashes or any(
        not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key) for key in hashes
    ):
        raise ValueError("Не удалось проверить архив канала.")
    return set(hashes)


def exclusions(entries, archive):
    keys = archive.copy()
    images = set()
    for entry in entries:
        keys.add(entry.get("title_digest") or text_digest(entry["title_key"]))
        keys.add(entry.get("text_digest") or text_digest(entry["text_key"]))
        images.add(entry["photo_sha256"])
    return keys, images


def fresh(card, keys):
    return all(text_digest(card[k]) not in keys for k in ("title", "text"))


def key_text(value):
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(re.findall(r"[^\W_]+", value))


def identity(card, digest):
    return {
        "title_key": key_text(card["title"]), "title_digest": text_digest(card["title"]),
        "text_key": key_text(card["text"]), "text_digest": text_digest(card["text"]),
        "photo_sha256": digest, "source_url": card["source_url"],
        **{key: card[key].strip() for key in ("author", "theme") if card.get(key, "").strip()},
    }


def editorial_labels(card):
    # Count missing labels together, so unlabeled posts also rotate.
    return [(field, key_text(card.get(field) or "")) for field in ("author", "theme")]


def evening_rank(entries, cards, target_slot):
    offset = target_slot % 8
    if offset not in EVENING_OFFSETS:
        return lambda card: (0,)

    # Older entries may lack the editorial labels. Recover known labels from their source card.
    catalog = {text_digest(card["text"]): card for card in cards}
    evening = Counter()
    same_time = Counter()
    all_times = Counter()
    for entry in entries:
        previous = entry["slot"]
        if not entry.get("run_id") or entry["status"] != "sent" or previous is None or previous >= target_slot:
            continue
        if previous % 8 not in EVENING_OFFSETS:
            continue
        original = catalog.get(entry.get("text_digest") or text_digest(entry["text_key"]), {})
        for key in editorial_labels({**original, **entry}):
            all_times[key] += 1
            same_time[key] += previous % 8 == offset
            evening[key] += previous // 8 == target_slot // 8

    def rank(card):
        labels = editorial_labels(card)
        # First spread authors and themes within an evening. Then prefer the slot where each
        # has appeared least relative to its total across the three evening times.
        return (
            sum(evening[k] for k in labels),
            sum(3 * same_time[k] - all_times[k] for k in labels),
            sum(same_time[k] for k in labels),
        )

    return rank


def choose_card(cards, state, archive, rank=None):
    keys, used_images = exclusions(state["entries"], archive)
    if rank is not None:
        cards = sorted(cards, key=rank)
    for card in cards:
        message = render(card)
        if not fresh(card, keys):
            continue
        photo, mime = photo_file(card)
        digest = hashlib.sha256(photo.read_bytes()).hexdigest()
        if digest not in used_images:
            return card, message, photo, mime, digest
    return None


def record_outcome(store, reservation_id, status, receipt=None):
    for _ in range(3):
        state, sha = store.read()
        entry = next((e for e in state["entries"] if e.get("reservation_id") == reservation_id), None)
        if entry is None or entry["status"] != "reserved":
            raise RuntimeError("Бронь публикации была изменена. Проверьте журнал.")
        entry["status"] = status
        if receipt is not None:
            entry.update(receipt)
        try:
            store.write(state, sha, f"Telegram {status}, slot {entry['slot']}")
            return
        except StateConflict:
            continue
    raise RuntimeError("GitHub занят. Бронь сохраняется, проверьте журнал и канал.")


def current_slot():
    return slot(datetime.now(timezone.utc))


def scheduled(cards, archive, store, target_slot, run_id):
    generated = []
    refill_attempted = False
    for _ in range(3):
        if current_slot() != target_slot:
            print("Время этой публикации прошло.")
            return
        state, sha = store.read()
        if any(e["slot"] == target_slot or e.get("run_id") == run_id for e in state["entries"]):
            print("Для этого запуска или времени уже существует запись.")
            return
        rank = evening_rank(state["entries"], cards, target_slot)
        choice = choose_card(cards + generated, state, archive, rank)
        if choice is None and not refill_attempted and os.environ.get("OPENAI_API_KEY"):
            from refill import next_card

            bot_token()
            keys, _ = exclusions(state["entries"], archive)
            for card in cards:
                keys.update(text_digest(card[k]) for k in ("title", "text"))
            refill_attempted = True
            card = next_card(lambda candidate: fresh(candidate, keys), rank=rank)
            if card is not None:
                generated.append(card)
                choice = choose_card(generated, state, archive, rank)
        if choice is None:
            print("Очередь новых карточек пуста.")
            return
        bot_token()
        card, message, photo, mime, digest = choice
        reservation_id = secrets.token_hex(16)
        state["entries"].append({
            **identity(card, digest), "reservation_id": reservation_id, "run_id": run_id,
            "slot": target_slot, "status": "reserved",
        })
        if current_slot() != target_slot:
            print("Время этой публикации прошло.")
            return
        try:
            store.write(state, sha, f"Reserve Telegram slot {target_slot}")
            break
        except StateConflict:
            continue
    else:
        raise RuntimeError("Очередь изменилась. Публикация пропущена.")

    if current_slot() != target_slot:
        record_outcome(store, reservation_id, "expired")
        return
    try:
        receipt = send(message, photo, mime)
    except UnverifiedMessage as error:
        record_outcome(store, reservation_id, "uncertain", error.receipt)
        raise
    except (OSError, RuntimeError, ValueError):
        record_outcome(store, reservation_id, "uncertain")
        raise
    record_outcome(store, reservation_id, "sent", receipt)


def repair_photo(cards, store, message_id):
    state, _ = store.read()
    matches = [e for e in state["entries"] if e.get("message_id") == message_id
               and e["status"] in {"sent", "uncertain"}]
    if len(matches) != 1:
        raise ValueError("Для этого сообщения в журнале нужна одна подтверждённая запись с его номером.")
    entry = matches[0]
    for card in cards:
        if (key_text(card["title"]), key_text(card["text"])) != (entry["title_key"], entry["text_key"]):
            continue
        photo, mime = photo_file(card)
        if hashlib.sha256(photo.read_bytes()).hexdigest() != entry["photo_sha256"]:
            continue
        receipt = send(render(card), photo, mime, edit_id=message_id)
        break
    else:
        raise ValueError("Исходная карточка с этим текстом и изображением не найдена.")
    for _ in range(3):
        state, sha = store.read()
        current = next((e for e in state["entries"] if e.get("message_id") == message_id), None)
        if (current is None or current["photo_sha256"] != entry["photo_sha256"]
                or current["status"] not in {"sent", "uncertain"}):
            raise RuntimeError("Запись в журнале изменилась во время редактирования.")
        current.update(status="sent", photo_width=receipt["photo_width"], photo_height=receipt["photo_height"])
        try:
            store.write(state, sha, f"Verify Telegram photo in message {message_id}")
            return
        except StateConflict:
            continue
    raise RuntimeError("Telegram подтвердил фотографию, но запись проверки в журнал не удалась.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--send", action="store_true", help="send the scheduled post")
    action.add_argument("--repair-photo", type=int, metavar="MESSAGE_ID", help="restore a published card in place")
    parser.add_argument("--created-at", help="UTC creation time of the scheduled workflow run")
    args = parser.parse_args()
    cards = json.loads(CATALOG.read_text(encoding="utf-8"))
    if not isinstance(cards, list):
        raise ValueError("Ожидается очередь карточек в cards.json.")
    if args.repair_photo is not None:
        if args.repair_photo <= 0:
            parser.error("--repair-photo requires a positive message ID")
        repair_photo(cards, RepositoryState(), args.repair_photo)
        return
    archive = archive_keys()
    if args.send:
        if not args.created_at or not os.environ.get("GITHUB_RUN_ID"):
            parser.error("--send requires --created-at and GITHUB_RUN_ID")
        created = datetime.fromisoformat(args.created_at.replace("Z", "+00:00"))
        if created.tzinfo is None:
            raise ValueError("Для времени запуска требуется часовой пояс.")
        scheduled(cards, archive, RepositoryState(), slot(created), os.environ["GITHUB_RUN_ID"])
    else:
        state = validate_state(json.loads(CATALOG.with_name(STATE).read_text(encoding="utf-8")))
        rank = evening_rank(state["entries"], cards, current_slot() + 1)
        choice = choose_card(cards, state, archive, rank)
        if choice is None:
            print("Очередь новых карточек пуста.")
            return
        _, message, photo, _, _ = choice
        print(f"{photo.relative_to(CATALOG.parent)}\n{json.dumps(message, ensure_ascii=False, indent=2)}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        sys.exit(str(error))
