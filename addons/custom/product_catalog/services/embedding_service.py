import base64
import gc
import hashlib
import io
import logging
import math
import os
import re
import resource
import threading

from PIL import Image

_logger = logging.getLogger(__name__)

# Directorio persistente dentro del volumen de Odoo (/var/lib/odoo -> odoo-web-data-dev)
PERSISTENT_AI_ROOT = os.environ.get("ODOO_AI_MODELS_DIR", "/var/lib/odoo/ai_models")
PERSISTENT_MODELS_DIR = os.path.join(PERSISTENT_AI_ROOT, "models")
PERSISTENT_HF_CACHE = os.path.join(PERSISTENT_AI_ROOT, "hf_cache")
PERSISTENT_ST_CACHE = os.path.join(PERSISTENT_AI_ROOT, "st_cache")

os.environ.setdefault("HF_HOME", PERSISTENT_HF_CACHE)
os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", PERSISTENT_ST_CACHE)
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

# Caché Singleton en memoria RAM del proceso Odoo
_MODEL_CACHE = {}
_MODEL_LOCK = threading.Lock()
_WARMUP_STARTED = False

DEFAULT_HF_MODEL = "google/embeddinggemma-2"
FALLBACK_OPEN_HF_MODEL = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"

SUPPORTED_DIMENSIONS = [1536, 1024, 768, 512, 384, 256, 128]


def _relax_virtual_memory_limit():
    """
    Odoo fija por defecto RLIMIT_AS a ~2.5GB de espacio virtual, lo que impide
    que PyTorch y safetensors hagan mmap() de archivos de modelos grandes
    (lanzando 'Cannot allocate memory (os error 12)').
    Esta función amplía RLIMIT_AS al máximo permitido por el sistema operativo.
    """
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY:
            resource.setrlimit(resource.RLIMIT_AS, (hard, hard))
        else:
            resource.setrlimit(resource.RLIMIT_AS, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    except Exception as exc:
        _logger.debug("No se pudo ajustar RLIMIT_AS: %s", exc)


def _ensure_persistent_dirs():
    """Garantiza que existan los directorios persistentes dentro del volumen /var/lib/odoo."""
    _relax_virtual_memory_limit()
    for path in (PERSISTENT_AI_ROOT, PERSISTENT_MODELS_DIR, PERSISTENT_HF_CACHE, PERSISTENT_ST_CACHE):
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as exc:
            _logger.warning("No se pudo crear directorio persistente %s: %s", path, exc)


def get_local_volume_model_dir(model_name: str) -> str:
    """
    Devuelve la ruta en el volumen persistente `/var/lib/odoo/ai_models/models/<slug>`
    donde se guarda el modelo para no tener que volver a descargarlo al reiniciar el contenedor.
    """
    _ensure_persistent_dirs()
    safe_slug = re.sub(r"[^a-zA-Z0-9._-]+", "__", (model_name or DEFAULT_HF_MODEL).strip())
    return os.path.join(PERSISTENT_MODELS_DIR, safe_slug)


def is_model_persisted_in_volume(local_dir: str) -> bool:
    """
    Verifica si un modelo ya fue descargado y guardado completamente en el volumen de Odoo.
    """
    if not local_dir or not os.path.isdir(local_dir):
        return False
    has_config = (
        os.path.isfile(os.path.join(local_dir, "config.json"))
        or os.path.isfile(os.path.join(local_dir, "modules.json"))
        or os.path.isfile(os.path.join(local_dir, "config_sentence_transformers.json"))
    )
    return has_config


def compute_sha256(content: bytes | str) -> str:
    """Calcula el hash SHA-256 de texto o bytes."""
    if not content:
        return ""
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def l2_normalize_and_fit_dimension(vector_list: list[float], target_dim: int) -> list[float]:
    """
    Aplica truncado Matryoshka (o ajuste de dimensión) y re-normalización L2
    para garantizar precisión óptima con distancia coseno (vector_cosine_ops).
    """
    if not vector_list:
        return [0.0] * target_dim

    if len(vector_list) >= target_dim:
        sliced = list(vector_list[:target_dim])
    else:
        sliced = list(vector_list) + [0.0] * (target_dim - len(vector_list))

    norm = math.sqrt(sum(x * x for x in sliced))
    if norm > 1e-12:
        sliced = [float(x / norm) for x in sliced]
    else:
        sliced = [float(x) for x in sliced]
    return sliced


def deterministic_vector_from_seed(seed_bytes: bytes, target_dim: int) -> list[float]:
    """
    Genera un vector unitario determinista (L2 normalizado) a partir de bytes semilla.
    Útil para modo de prueba rápida (unit tests).
    """
    raw_vals = []
    counter = 0
    while len(raw_vals) < target_dim:
        digest = hashlib.sha256(seed_bytes + f":{counter}".encode("utf-8")).digest()
        for i in range(0, len(digest), 2):
            if len(raw_vals) >= target_dim:
                break
            uint16_val = int.from_bytes(digest[i:i + 2], byteorder="big", signed=False)
            mapped = (uint16_val / 32767.5) - 1.0
            raw_vals.append(mapped)
        counter += 1
    return l2_normalize_and_fit_dimension(raw_vals, target_dim)


def get_pdf_total_pages(pdf_bytes: bytes) -> int:
    """Obtiene rápidamente el número total de páginas del PDF sin renderizar imágenes."""
    import pymupdf as fitz
    try:
        fitz.TOOLS.mupdf_display_errors(False)
    except Exception:
        pass
    if not pdf_bytes:
        raise ValueError("El contenido del archivo PDF está vacío.")
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = len(doc)
    doc.close()
    return total


def iter_pdf_page_chunks(pdf_bytes: bytes, dpi: int = 120, skip_pages: set[int] | None = None):
    """
    Generador en streaming que extrae y renderiza UNA sola página a la vez,
    liberando la memoria de PyMuPDF inmediatamente después de cada página.
    Ideal para catálogos grandes (50 - 300+ páginas) sin saturar la memoria RAM.
    """
    try:
        import pymupdf as fitz
    except ImportError as exc:
        raise RuntimeError(
            "La librería PyMuPDF (pymupdf) no está instalada. Ejecute: pip install pymupdf"
        ) from exc

    try:
        fitz.TOOLS.mupdf_display_errors(False)
    except Exception:
        pass

    if not pdf_bytes:
        raise ValueError("El contenido del archivo PDF está vacío.")

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"No se pudo abrir el documento PDF: {exc}") from exc

    total_pages = len(doc)
    try:
        for idx in range(total_pages):
            page_num = idx + 1
            if skip_pages and page_num in skip_pages:
                continue

            page = doc.load_page(idx)

            raw_text = (page.get_text("text") or "").strip()
            text_blocks = page.get_text("blocks") or []
            embedded_images = page.get_images(full=True) or []

            pix = page.get_pixmap(dpi=dpi, alpha=False)
            png_bytes = pix.tobytes("png")
            width_px, height_px = pix.width, pix.height
            pix = None  # Liberar pixmap nativo inmediatamente

            png_b64 = base64.b64encode(png_bytes).decode("ascii")
            pil_img = Image.open(io.BytesIO(png_bytes)).convert("RGB")

            words = raw_text.split()
            page_metadata = {
                "page_number": page_num,
                "total_pages": total_pages,
                "width_pt": round(float(page.rect.width), 2),
                "height_pt": round(float(page.rect.height), 2),
                "render_width_px": width_px,
                "render_height_px": height_px,
                "render_dpi": dpi,
                "char_count": len(raw_text),
                "word_count": len(words),
                "text_block_count": len(text_blocks),
                "embedded_image_count": len(embedded_images),
            }

            yield {
                "page_number": page_num,
                "total_pages": total_pages,
                "raw_text": raw_text,
                "page_image_png_bytes": png_bytes,
                "page_image_b64": png_b64,
                "page_pil_image": pil_img,
                "page_metadata": page_metadata,
            }

            # Limpiar cachés internos de MuPDF y recolectar basura tras cada página
            try:
                fitz.TOOLS.store_shrink(100)
            except Exception:
                pass
            gc.collect()
    finally:
        doc.close()


def extract_pdf_page_chunks(pdf_bytes: bytes, dpi: int = 120) -> list[dict]:
    """Wrapper compatible que retorna la lista de páginas usando el generador."""
    return list(iter_pdf_page_chunks(pdf_bytes, dpi=dpi))


def _load_or_download_to_volume(model_name: str, token: str | None) -> object:
    """
    1. Si el modelo ya existe en `/var/lib/odoo/ai_models/models/<slug>`, lo carga
       directamente desde el disco local (`local_files_only=True`) sin descargar nada.
    2. Si aún no existe en el volumen, lo descarga de Hugging Face y lo guarda
       permanentemente en `/var/lib/odoo/ai_models/models/<slug>`.
    """
    from sentence_transformers import SentenceTransformer
    import torch

    local_dir = get_local_volume_model_dir(model_name)

    # Caso 1: Ya está persistido en el volumen de Odoo -> Carga instantánea desde disco
    if is_model_persisted_in_volume(local_dir):
        _logger.info(
            "Cargando modelo '%s' desde el volumen persistente de Odoo (%s) [sin descarga]...",
            model_name,
            local_dir,
        )
        try:
            return SentenceTransformer(
                local_dir,
                local_files_only=True,
                trust_remote_code=True,
                model_kwargs={"torch_dtype": torch.bfloat16, "low_cpu_mem_usage": True},
            )
        except Exception:
            return SentenceTransformer(
                local_dir,
                local_files_only=True,
                trust_remote_code=True,
            )

    # Caso 2: Primera vez -> Descargar desde Hugging Face y guardar en el volumen de Odoo
    _logger.info(
        "Modelo '%s' no encontrado en volumen local. Descargando por primera vez y guardando en %s ...",
        model_name,
        local_dir,
    )
    try:
        model = SentenceTransformer(
            model_name,
            token=token,
            trust_remote_code=True,
            cache_folder=PERSISTENT_ST_CACHE,
            model_kwargs={"torch_dtype": torch.bfloat16, "low_cpu_mem_usage": True},
        )
    except Exception:
        model = SentenceTransformer(
            model_name,
            token=token,
            trust_remote_code=True,
            cache_folder=PERSISTENT_ST_CACHE,
        )

    try:
        os.makedirs(local_dir, exist_ok=True)
        model.save(local_dir)
        _logger.info(
            "Modelo '%s' guardado exitosamente en el volumen persistente de Odoo: %s",
            model_name,
            local_dir,
        )
    except Exception as save_err:
        _logger.warning(
            "El modelo '%s' se cargó en memoria y en caché HF (%s), pero model.save(%s) reportó: %s",
            model_name,
            PERSISTENT_HF_CACHE,
            local_dir,
            save_err,
        )

    return model


def get_hf_sentence_transformer(
    model_name: str = DEFAULT_HF_MODEL,
    hf_token: str | None = None,
    allow_open_fallback: bool = True,
) -> tuple[object, str, str]:
    """
    Obtiene o inicializa un modelo SentenceTransformer desde:
    1) Caché en memoria RAM (`_MODEL_CACHE`), o
    2) Volumen persistente de Odoo (`/var/lib/odoo/ai_models/models/...`), o
    3) Descarga inicial desde Hugging Face guardándolo en el volumen persistente.
    Retorna: (model_instance, effective_model_name, warning_message)
    """
    token = (hf_token or os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or "").strip() or None
    cache_key = model_name

    with _MODEL_LOCK:
        if cache_key in _MODEL_CACHE:
            cached_model, effective_name, warn_msg = _MODEL_CACHE[cache_key]
            return cached_model, effective_name, warn_msg

        try:
            model = _load_or_download_to_volume(model_name, token)
            _MODEL_CACHE[cache_key] = (model, model_name, "")
            return model, model_name, ""
        except Exception as primary_err:
            err_str = str(primary_err)
            _logger.warning("No se pudo cargar el modelo primario '%s': %s", model_name, err_str)
            if not allow_open_fallback or model_name == FALLBACK_OPEN_HF_MODEL:
                raise RuntimeError(
                    f"No se pudo cargar el modelo de Hugging Face '{model_name}'. "
                    f"Si es un modelo con acceso restringido (Gated Repo en Hugging Face), "
                    f"configure la variable de entorno HF_TOKEN o el parámetro del sistema "
                    f"'product_catalog.hf_token'. Detalle: {err_str}"
                ) from primary_err

            warning_msg = (
                f"Aviso: No se pudo descargar '{model_name}' directamente ({err_str[:180]}). "
                f"Se cargó y persistió en el volumen el modelo abierto '{FALLBACK_OPEN_HF_MODEL}' (768d). "
                f"Configure 'product_catalog.hf_token' o HF_TOKEN para descargar '{model_name}'."
            )
            if FALLBACK_OPEN_HF_MODEL in _MODEL_CACHE:
                fb_model, _, _ = _MODEL_CACHE[FALLBACK_OPEN_HF_MODEL]
            else:
                fb_model = _load_or_download_to_volume(FALLBACK_OPEN_HF_MODEL, token=None)
                _MODEL_CACHE[FALLBACK_OPEN_HF_MODEL] = (fb_model, FALLBACK_OPEN_HF_MODEL, "")
            return fb_model, FALLBACK_OPEN_HF_MODEL, warning_msg


def warmup_model_on_service_start(
    model_name: str = DEFAULT_HF_MODEL,
    hf_token: str | None = None,
    allow_open_fallback: bool = True,
):
    """
    Se ejecuta automáticamente al iniciar el módulo/servicio de Odoo.
    Lanza un hilo en segundo plano que verifica si el modelo ya está en el volumen
    `/var/lib/odoo/ai_models/models/...`; si no está, lo descarga y lo persiste en el volumen,
    y lo deja pre-cargado en memoria RAM (`_MODEL_CACHE`) listo para usar.
    """
    global _WARMUP_STARTED
    with _MODEL_LOCK:
        if _WARMUP_STARTED:
            return
        _WARMUP_STARTED = True

    def _warmup_worker():
        try:
            _ensure_persistent_dirs()
            local_dir = get_local_volume_model_dir(model_name)
            already_in_volume = is_model_persisted_in_volume(local_dir)
            _logger.info(
                "[product_catalog] Inicializando modelo '%s' al arranque del servicio "
                "(persistido en volumen %s: %s)...",
                model_name,
                local_dir,
                "SÍ" if already_in_volume else "NO - se descargará y guardará en el volumen",
            )
            _, effective_name, warn_msg = get_hf_sentence_transformer(
                model_name=model_name,
                hf_token=hf_token,
                allow_open_fallback=allow_open_fallback,
            )
            if warn_msg:
                _logger.info("[product_catalog] %s", warn_msg)
            _logger.info(
                "[product_catalog] Modelo '%s' listo en memoria y persistido en volumen '%s'.",
                effective_name,
                get_local_volume_model_dir(effective_name),
            )
        except Exception as exc:
            _logger.warning("[product_catalog] No se pudo pre-cargar el modelo al inicio del servicio: %s", exc)

    t = threading.Thread(target=_warmup_worker, name="odoo-catalog-model-warmup", daemon=True)
    t.start()


def format_gemma_document_prompt(
    catalog_title: str,
    partner_name: str,
    page_number: int,
    total_pages: int,
    tags_list: list[str],
    raw_page_text: str,
) -> str:
    """
    Construye el texto enriquecido de la página siguiendo el formato de prompt
    recomendado para google/embeddinggemma-2:
    'task: search result | title: ... | text: ...'
    """
    tags_str = ", ".join(tags_list) if tags_list else "General"
    header_title = f"{catalog_title} | Proveedor: {partner_name} | Pág. {page_number}/{total_pages} | Tags: {tags_str}"
    body_text = raw_page_text.strip() if raw_page_text and raw_page_text.strip() else (
        f"Página visual {page_number} del catálogo {catalog_title} del proveedor {partner_name} ({tags_str})."
    )
    return f"task: search result | title: {header_title} | text: {body_text}"


def format_gemma_query_prompt(query_text: str) -> str:
    """Construye el prompt de búsqueda para google/embeddinggemma-2."""
    clean_q = (query_text or "").strip()
    if clean_q.startswith("task:"):
        return clean_q
    return f"task: search query | query: {clean_q}"


def generate_text_embedding(
    formatted_text: str,
    target_dim: int = 768,
    model_name: str = DEFAULT_HF_MODEL,
    hf_token: str | None = None,
    allow_open_fallback: bool = True,
    use_deterministic_test_mode: bool = False,
) -> tuple[list[float], str, str]:
    """
    Genera el vector de embedding para un texto (página o consulta) y lo ajusta
    a target_dim mediante truncado Matryoshka y normalización L2.
    Retorna: (vector_floats, effective_model_name, warning_msg)
    """
    if use_deterministic_test_mode:
        vec = deterministic_vector_from_seed(formatted_text.encode("utf-8"), target_dim)
        return vec, model_name, ""

    model, effective_model, warn_msg = get_hf_sentence_transformer(
        model_name=model_name,
        hf_token=hf_token,
        allow_open_fallback=allow_open_fallback,
    )
    raw_emb = model.encode(formatted_text, normalize_embeddings=True)
    if hasattr(raw_emb, "tolist"):
        raw_list = raw_emb.tolist()
    else:
        raw_list = list(raw_emb)

    if raw_list and isinstance(raw_list[0], (list, tuple)):
        raw_list = raw_list[0]

    fitted_vec = l2_normalize_and_fit_dimension(raw_list, target_dim)
    return fitted_vec, effective_model, warn_msg


def generate_image_embedding(
    pil_image: Image.Image,
    fallback_caption_text: str,
    target_dim: int = 768,
    model_name: str = DEFAULT_HF_MODEL,
    hf_token: str | None = None,
    allow_open_fallback: bool = True,
    use_deterministic_test_mode: bool = False,
) -> tuple[list[float], str, str]:
    """
    Genera el vector de embedding para la imagen de una página de catálogo.
    """
    if use_deterministic_test_mode:
        buf = io.BytesIO()
        pil_image.save(buf, format="PNG")
        vec = deterministic_vector_from_seed(buf.getvalue(), target_dim)
        return vec, model_name, ""

    model, effective_model, warn_msg = get_hf_sentence_transformer(
        model_name=model_name,
        hf_token=hf_token,
        allow_open_fallback=allow_open_fallback,
    )
    try:
        raw_emb = model.encode(pil_image, normalize_embeddings=True)
        if hasattr(raw_emb, "tolist"):
            raw_list = raw_emb.tolist()
        else:
            raw_list = list(raw_emb)
        if raw_list and isinstance(raw_list[0], (list, tuple)):
            raw_list = raw_list[0]
        fitted_vec = l2_normalize_and_fit_dimension(raw_list, target_dim)
        return fitted_vec, effective_model, warn_msg
    except Exception as img_err:
        _logger.info(
            "El modelo activo '%s' no codificó PIL.Image directamente (%s); "
            "codificando representación descriptiva de la imagen de página.",
            effective_model,
            img_err,
        )
        return generate_text_embedding(
            formatted_text=fallback_caption_text,
            target_dim=target_dim,
            model_name=effective_model,
            hf_token=hf_token,
            allow_open_fallback=allow_open_fallback,
            use_deterministic_test_mode=False,
        )
