"""Verify the displayed photo in a confirmed Telegram Rich Message."""

import io
import json
import re
from http.client import HTTPException
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PIL import Image, ImageChops, ImageOps, ImageStat


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read(opener, request, limit):
    with opener.open(request, timeout=20) as response:
        data = response.read(limit + 1)
    if not data or len(data) > limit:
        raise ValueError("Недопустимый размер ответа.")
    return data


def _thumbnail(source):
    with Image.open(source, formats=("JPEG", "PNG")) as picture:
        if picture.width + picture.height > 10_000:
            raise ValueError("Недопустимый размер фотографии.")
        oriented = ImageOps.exif_transpose(picture)
        thumbnail = oriented.convert("RGB").resize((64, 64), Image.Resampling.LANCZOS)
        return oriented.size, thumbnail


def _display_size(size, original):
    if not isinstance(size, dict) or not all(
        isinstance(size.get(k), str) and size[k].strip() for k in ("file_id", "file_unique_id")
    ):
        return False
    w, h = size.get("width"), size.get("height")
    if type(w) is not int or type(h) is not int or min(w, h) <= 0 or w + h > 10_000:
        return False
    aspect = (w / h) / (original[0] / original[1])
    return max(w, h) >= 0.95 * min(800, max(original)) and 0.95 <= aspect <= 1.05


def verify_photo(blocks, source, token):
    """Download the largest matching PhotoSize and return its visually verified dimensions."""
    if not (isinstance(blocks, list) and blocks and isinstance(blocks[0], dict)
            and blocks[0].get("type") == "photo" and isinstance(blocks[0].get("photo"), list)):
        raise RuntimeError("В ответе Telegram отсутствует блок фотографии.")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise RuntimeError("Не задан токен для проверки фотографии Telegram.")
    try:
        original_size, original = _thumbnail(source)
        sizes = [item for item in blocks[0]["photo"] if _display_size(item, original_size)]
        if not sizes:
            raise RuntimeError("Telegram не вернул фотографию достаточного разрешения.")
        best = max(sizes, key=lambda item: item["width"] * item["height"])
        opener = build_opener(_NoRedirect())
        lookup = Request(
            f"https://api.telegram.org/bot{token}/getFile",
            data=json.dumps({"file_id": best["file_id"]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        reply = json.loads(_read(opener, lookup, 64 * 1024))
        file = reply.get("result") if isinstance(reply, dict) and reply.get("ok") is True else None
        if not isinstance(file, dict) or file.get("file_unique_id") != best["file_unique_id"]:
            raise RuntimeError("Telegram не подтвердил файл фотографии.")
        path = file.get("file_path")
        if not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,1024}", path):
            raise RuntimeError("Telegram не вернул допустимый путь к фотографии.")
        if any(segment in ("", ".", "..") for segment in path.split("/")):
            raise RuntimeError("Telegram вернул недопустимый путь к фотографии.")
        url = f"https://api.telegram.org/file/bot{token}/{path}"
        received_size, received = _thumbnail(io.BytesIO(_read(opener, Request(url), 20_000_000)))
        if received_size != (best["width"], best["height"]):
            raise RuntimeError("Размеры скачанной фотографии расходятся с ответом Telegram.")
        contrast = max(ImageStat.Stat(original).stddev)
        difference = ImageStat.Stat(ImageChops.difference(original, received))
        if (max(ImageStat.Stat(received).stddev) < 3
                or max(difference.mean) > max(4, min(15, contrast * 0.3))
                or max(difference.rms) > max(7, min(25, contrast * 0.45))):
            raise RuntimeError("Скачанная фотография не совпадает с исходным изображением.")
        return received_size
    except (OSError, ValueError, SyntaxError, HTTPException, Image.DecompressionBombError):
        # urllib exceptions and malformed redirect URLs can include confidential identifiers.
        raise RuntimeError("Не удалось скачать или прочитать фотографию для проверки.") from None
