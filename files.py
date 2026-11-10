"""Pièces jointes de la discussion : vérification et lecture du texte (PDF, DOCX, ODT, TXT)."""
import base64
import io
import re
import zipfile

MAX_BYTES = 10 * 1024 * 1024
MAX_CHARS = 15000


class ImportError_(Exception):
    pass


def decode(file):
    """Pièce jointe reçue de la page ({name, mime, data en base64}) -> {name, mime, data en octets}."""
    if not isinstance(file, dict) or not file.get("data"):
        return None
    try:
        data = base64.b64decode(file["data"], validate=False)
    except ValueError:
        raise ImportError_("Pièce jointe illisible.")
    if not data:
        raise ImportError_("La pièce jointe est vide.")
    if len(data) > MAX_BYTES:
        raise ImportError_("Pièce jointe trop volumineuse (10 Mo au plus).")
    name = re.sub(r"[\x00-\x1f\x7f]", "", str(file.get("name") or "document").replace("\\", "/").split("/")[-1])[:150]
    return {"name": name or "document", "mime": str(file.get("mime") or "application/octet-stream")[:100], "data": data}


def is_image(file):
    return file["mime"].startswith("image/")


def _xml_text(raw):
    raw = re.sub(r"<(?:w:tab|text:tab)[^>]*/>", "\t", raw)
    raw = re.sub(r"</(?:w:p|text:p|text:h)>|<(?:w:br|text:line-break)[^>]*/>", "\n", raw)
    raw = re.sub(r"<[^>]+>", "", raw)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'")):
        raw = raw.replace(a, b)
    return raw


def extract_text(name, data):
    """Texte brut d'un CV (.pdf, .docx, .odt, .txt, .md)."""
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if ext == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ImportError_("Lecture des PDF indisponible : lance `pip install -r requirements.txt`.")
        try:
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:15])
        except Exception as e:
            raise ImportError_(f"{name} : PDF illisible ({e}).")
        if len(text.strip()) < 100:
            raise ImportError_(f"{name} : aucun texte trouvé (PDF scanné ou image ?). "
                               "Exporte-le en PDF texte ou en .docx.")
    elif ext in ("docx", "odt"):
        member = "word/document.xml" if ext == "docx" else "content.xml"
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                text = _xml_text(z.read(member).decode("utf-8", "replace"))
        except (zipfile.BadZipFile, KeyError):
            raise ImportError_(f"{name} : fichier {ext} illisible.")
    elif ext in ("txt", "md", ""):
        text = data.decode("utf-8", "replace")
    else:
        raise ImportError_(f"{name} : format non géré (PDF, DOCX, ODT, TXT ou MD).")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()[:MAX_CHARS]
