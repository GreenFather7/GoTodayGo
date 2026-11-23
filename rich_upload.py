"""Encode the channel's rich blocks as HTML with an explicit multipart photo binding."""

from html import escape
from urllib.parse import urlsplit


def _inline(value):
    if isinstance(value, str):
        return escape(value, quote=False).replace("\n", "<br>")
    if isinstance(value, list):
        return "".join(_inline(part) for part in value)
    if isinstance(value, dict) and value.keys() == {"type", "text", "url"} and value["type"] == "url":
        url = value["url"]
        if not isinstance(url, str) or not isinstance(value["text"], str):
            raise ValueError("Некорректная ссылка в тексте поста.")
        target = urlsplit(url)
        if target.scheme != "https" or not target.hostname or target.username is not None:
            raise ValueError("Для ссылки в посте требуется адрес HTTPS.")
        return f'<a href="{escape(url, quote=True)}">{_inline(value["text"])}</a>'
    raise ValueError("Неподдерживаемый элемент текста поста.")


def rich_upload(message):
    """Translate publish.render() output; the caller uploads a form part named photo."""
    blocks = message.get("blocks") if isinstance(message, dict) else None
    if not isinstance(blocks, list) or not all(isinstance(b, dict) for b in blocks):
        raise ValueError("Неверная структура сообщения.")
    types = [b.get("type") for b in blocks]
    if types != ["photo", "heading", "paragraph", "footer"]:
        raise ValueError("Проверьте порядок блоков поста.")
    photo = {"type": "photo", "media": "attach://photo"}
    if blocks[0] != {"type": "photo", "photo": photo}:
        raise ValueError("Ожидается вложенный файл фотографии.")
    if blocks[1].get("size") != 1:
        raise ValueError("Ожидается заголовок первого уровня.")
    tags = {"heading": "h1", "paragraph": "p", "footer": "footer"}
    content = ['<img src="tg://photo?id=illustration"/>']
    for block in blocks[1:]:
        tag = tags[block["type"]]
        content.append(f'<{tag}>{_inline(block.get("text"))}</{tag}>')
    return {"html": "".join(content), "media": [{"id": "illustration", "media": photo}]}
