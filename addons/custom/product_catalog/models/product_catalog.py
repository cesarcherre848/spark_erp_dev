# -*- coding: utf-8 -*-
import base64
import json
import logging
import threading
import traceback

import odoo
from odoo import models, fields, api, _
from odoo.exceptions import ValidationError, UserError

from ..services.embedding_service import (
    DEFAULT_HF_MODEL,
    get_pdf_total_pages,
    iter_pdf_page_chunks,
    format_gemma_document_prompt,
    generate_text_embedding,
    generate_image_embedding,
    compute_sha256,
    warmup_model_on_service_start,
    get_local_volume_model_dir,
    is_model_persisted_in_volume,
)

_logger = logging.getLogger(__name__)

_ACTIVE_CATALOG_WORKERS = set()
_CANCEL_CATALOG_WORKERS = set()
_WORKER_LOCK = threading.Lock()




class ProductCatalog(models.Model):
    _name = 'product.catalog'
    _description = 'Catálogo de Productos de Proveedor (PDF)'
    _order = 'date_start desc, id desc'

    name = fields.Char(
        string='Referencia',
        compute='_compute_name',
        store=True,
        index=True,
    )
    title = fields.Char(
        string='Título del Catálogo',
        required=True,
        index=True,
        help='Nombre identificador del catálogo o campaña (ej. Catálogo Campaña C10 - Invierno).',
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Proveedor',
        domain="[('supplier_rank', '>', 0)]",
        required=True,
        ondelete='cascade',
        index=True,
        help='Proveedor o fabricante propietario del catálogo.',
    )
    company_id = fields.Many2one(
        'res.company',
        string='Compañía',
        default=lambda self: self.env.company,
        required=True,
    )
    description = fields.Text(
        string='Descripción / Notas',
        help='Observaciones generales sobre familias de productos, descuentos o condiciones del catálogo.',
    )

    # Documento PDF
    pdf_file = fields.Binary(
        string='Documento PDF',
        attachment=True,
        required=True,
        help='Archivo PDF del catálogo de productos.',
    )
    pdf_filename = fields.Char(
        string='Nombre del Archivo PDF',
    )
    page_count = fields.Integer(
        string='Total Páginas',
        readonly=True,
        default=0,
    )

    # Vigencia Temporal y Etiquetas (product.tag)
    tag_ids = fields.Many2many(
        'product.tag',
        'product_catalog_product_tag_rel',
        'catalog_id',
        'tag_id',
        string='Etiquetas de Producto',
        help='Etiquetas para filtrar fácilmente por campaña, categoría o familia de productos.',
    )
    date_start = fields.Datetime(
        string='Inicio de Vigencia',
        required=True,
        default=fields.Datetime.now,
        index=True,
    )
    date_end = fields.Datetime(
        string='Fin de Vigencia',
        index=True,
        help='Fecha y hora opcional en la que expira la validez del catálogo.',
    )
    state = fields.Selection([
        ('upcoming', 'Por Iniciar'),
        ('active', 'Vigente'),
        ('expired', 'Vencido'),
    ], string='Vigencia', compute='_compute_state', search='_search_state')

    is_valid = fields.Boolean(
        string='Vigente Ahora',
        compute='_compute_is_valid',
    )
    active = fields.Boolean(
        string='Activo',
        default=True,
    )

    # Estado de Chunking y Vectorización IA
    index_state = fields.Selection([
        ('pending', 'No Iniciado'),
        ('processing', 'En Curso'),
        ('done', 'Finalizado'),
        ('error', 'Error'),
    ], string='Estado de Chunking', default='pending', required=True, index=True, copy=False)

    indexed_pages = fields.Integer(
        string='Páginas Procesadas',
        readonly=True,
        default=0,
        copy=False,
    )
    index_progress = fields.Float(
        string='Progreso (%)',
        compute='_compute_index_progress',
        store=True,
    )
    last_indexed_at = fields.Datetime(
        string='Última Vectorización',
        readonly=True,
        copy=False,
    )
    index_log = fields.Text(
        string='Registro de Procesamiento IA',
        readonly=True,
        copy=False,
    )

    # Configuración del Modelo de Embeddings
    ai_provider = fields.Selection([
        ('huggingface', 'HuggingFace Local'),
        ('gemini', 'Google Gemini'),
        ('openai', 'OpenAI'),
        ('ollama', 'Ollama'),
        ('other', 'Otro'),
    ], string='Proveedor IA', default='huggingface', required=True)

    ai_model = fields.Char(
        string='Modelo de Embeddings',
        default=DEFAULT_HF_MODEL,
        required=True,
        help='ID del modelo en Hugging Face. Por defecto: google/embeddinggemma-2',
    )
    target_dimension = fields.Selection([
        ('768', '768 dimensiones (Nativo EmbeddingGemma-2)'),
        ('512', '512 dimensiones (Matryoshka MRL)'),
        ('256', '256 dimensiones (Matryoshka MRL)'),
        ('128', '128 dimensiones (Matryoshka MRL Compacto)'),
        ('384', '384 dimensiones'),
        ('1024', '1024 dimensiones'),
        ('1536', '1536 dimensiones'),
    ], string='Tamaño del Vector', default='768', required=True)

    embedding_mode = fields.Selection([
        ('text', 'Texto por Página + Captura de Imagen PNG (Recomendado en CPU)'),
        ('both', 'Multimodal Completo: Vector Texto + Vector Imagen (Recomendado con GPU)'),
        ('image', 'Solo Vector de Imagen por Página'),
    ], string='Modo de Chunking por Página', default='text', required=True)

    render_dpi = fields.Integer(
        string='Resolución de Captura (DPI)',
        default=150,
        help='Resolución en DPI para capturar la imagen PNG de cada página del PDF.',
    )
    auto_index = fields.Boolean(
        string='Iniciar Chunking al Subir PDF',
        default=True,
        help='Si está activo, al crear el registro o cambiar el PDF se iniciará automáticamente el chunking por página.',
    )
    allow_open_fallback = fields.Boolean(
        string='Permitir Respaldo HF Abierto',
        default=True,
        help='Si el modelo requiere HF_TOKEN y aún no se ha configurado, usa automáticamente un modelo multilingüe abierto de 768d para no detener la operación.',
    )

    # Relación con la tabla de Embeddings por Página
    embedding_ids = fields.One2many(
        'product.catalog.embedding',
        'catalog_id',
        string='Chunks y Embeddings por Página',
    )
    embedding_count = fields.Integer(
        string='Total Embeddings',
        compute='_compute_embedding_count',
    )
    model_volume_path = fields.Char(
        string='Ruta en Volumen de Odoo',
        compute='_compute_model_volume_info',
    )
    model_persisted_in_volume = fields.Boolean(
        string='Guardado en Volumen Local',
        compute='_compute_model_volume_info',
    )

    def _register_hook(self):
        """
        Hook ejecutado automáticamente por Odoo al iniciar el servicio/registro del módulo.
        1. Pre-carga y persiste el modelo en el volumen de Odoo (/var/lib/odoo/ai_models/models).
        2. Si algún catálogo quedó en estado 'processing' tras un reinicio del contenedor, lo reanuda.
        """
        res = super()._register_hook()
        if (
            not odoo.tools.config.get('test_enable')
            and not self.env.context.get('test_enable')
            and not odoo.tools.config.get('stop_after_init')
        ):
            try:
                hf_token = self.env['ir.config_parameter'].sudo().get_param('product_catalog.hf_token', default='')
            except Exception:
                hf_token = ''
            warmup_model_on_service_start(
                model_name=DEFAULT_HF_MODEL,
                hf_token=hf_token,
                allow_open_fallback=True,
            )
            try:
                interrupted = self.sudo().search([('index_state', '=', 'processing')]).filtered(lambda c: bool(c.pdf_file))
                for cat in interrupted:
                    _logger.info("[product_catalog] Reanudando chunking incremental del catálogo ID %s (%s)...", cat.id, cat.title)
                    cat._trigger_chunking(async_mode=True, resume=True, from_startup=True)
            except Exception as exc:
                _logger.warning("[product_catalog] No se pudieron verificar catálogos en curso: %s", exc)
        return res

    @api.depends('ai_model')
    def _compute_model_volume_info(self):
        for rec in self:
            local_dir = get_local_volume_model_dir(rec.ai_model or DEFAULT_HF_MODEL)
            rec.model_volume_path = local_dir
            rec.model_persisted_in_volume = is_model_persisted_in_volume(local_dir)

    @api.depends('title', 'partner_id.name')
    def _compute_name(self):
        for rec in self:
            partner_name = rec.partner_id.name if rec.partner_id else _('Sin Proveedor')
            title = rec.title or _('Sin Título')
            rec.name = f"[{title}] {partner_name}"

    @api.depends('date_start', 'date_end')
    def _compute_state(self):
        now = fields.Datetime.now()
        for rec in self:
            if not rec.date_start or rec.date_start > now:
                rec.state = 'upcoming'
            elif rec.date_end and rec.date_end < now:
                rec.state = 'expired'
            else:
                rec.state = 'active'

    @api.depends('state')
    def _compute_is_valid(self):
        for rec in self:
            rec.is_valid = (rec.state == 'active')

    @api.depends('embedding_ids')
    def _compute_embedding_count(self):
        for rec in self:
            rec.embedding_count = len(rec.embedding_ids)

    @api.depends('page_count', 'indexed_pages', 'index_state')
    def _compute_index_progress(self):
        for rec in self:
            if rec.index_state == 'done':
                rec.index_progress = 100.0
            elif rec.page_count and rec.page_count > 0:
                rec.index_progress = min(100.0, round((rec.indexed_pages / rec.page_count) * 100.0, 1))
            else:
                rec.index_progress = 0.0

    def _search_state(self, operator, value):
        now = fields.Datetime.now()
        if operator in ('=', '!='):
            if value == 'active':
                domain = [
                    ('date_start', '<=', now),
                    '|',
                    ('date_end', '=', False),
                    ('date_end', '>=', now),
                ]
            elif value == 'upcoming':
                domain = [('date_start', '>', now)]
            elif value == 'expired':
                domain = [
                    ('date_end', '!=', False),
                    ('date_end', '<', now),
                ]
            else:
                domain = []
            if operator == '!=':
                domain = ['!'] + domain
            return domain
        return []

    @api.constrains('date_start', 'date_end')
    def _check_validity_dates(self):
        for rec in self:
            if rec.date_start and rec.date_end and rec.date_end < rec.date_start:
                raise ValidationError(_('La fecha de fin no puede ser anterior a la fecha de inicio de vigencia.'))

    @api.constrains('pdf_file', 'pdf_filename')
    def _check_pdf_file(self):
        for rec in self:
            if rec.pdf_filename and not rec.pdf_filename.lower().endswith('.pdf'):
                raise ValidationError(_('El archivo subido debe tener extensión .pdf'))
            if rec.pdf_file:
                raw = base64.b64decode(rec.pdf_file[:64]) if isinstance(rec.pdf_file, (bytes, str)) else b""
                if raw and not raw.startswith(b'%PDF'):
                    raise ValidationError(_('El archivo subido no tiene una cabecera PDF válida (%PDF).'))

    def is_valid_at(self, check_datetime=None):
        """Determina si el catálogo está vigente en una fecha/hora determinada."""
        self.ensure_one()
        if not self.active:
            return False
        if not check_datetime:
            check_datetime = fields.Datetime.now()
        if self.date_start > check_datetime:
            return False
        if self.date_end and self.date_end < check_datetime:
            return False
        return True

    # -------------------------------------------------------------------------
    # Hooks automáticos al subir o actualizar el PDF
    # -------------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if rec.pdf_file and rec.auto_index:
                rec._trigger_chunking(async_mode=not self.env.context.get('test_enable'))
        return records

    def write(self, vals):
        res = super().write(vals)
        if 'pdf_file' in vals and vals.get('pdf_file'):
            for rec in self:
                if rec.auto_index:
                    rec._trigger_chunking(async_mode=not self.env.context.get('test_enable'))
                else:
                    rec.with_context(skip_auto_index=True).write({
                        'index_state': 'pending',
                        'indexed_pages': 0,
                    })
        return res

    # -------------------------------------------------------------------------
    # Acciones Manuales desde Botones de la Vista Odoo
    # -------------------------------------------------------------------------
    def action_start_chunking(self):
        """
        Botón manual en Odoo: inicia o re-ejecuta el chunking por página
        y la generación de embeddings para el catálogo.
        """
        for rec in self:
            if not rec.pdf_file:
                raise UserError(_('Debe subir un archivo PDF antes de iniciar el chunking.'))
            run_async = not (
                self.env.context.get('test_enable')
                or self.env.context.get('sync_chunking')
                or odoo.tools.config.get('test_enable')
            )
            rec._trigger_chunking(async_mode=run_async)
        return True

    def action_Start_chunking_sync(self):
        """Ejecuta el chunking de forma síncrona."""
        for rec in self:
            rec._run_chunking_pipeline()
        return True

    def action_reset_chunking(self):
        """Permite detener y reiniciar manualmente el estado si un proceso está en curso."""
        for rec in self:
            with _WORKER_LOCK:
                _CANCEL_CATALOG_WORKERS.add(rec.id)
                _ACTIVE_CATALOG_WORKERS.discard(rec.id)
        self.with_context(skip_auto_index=True).write({
            'index_state': 'pending',
            'index_log': _('Proceso detenido y estado reiniciado manualmente por el usuario.'),
        })
        return True

    def action_view_embeddings(self):
        """Smart Button para abrir los chunks/embeddings generados de este catálogo."""
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'product_catalog.action_product_catalog_embedding'
        )
        action['domain'] = [('catalog_id', '=', self.id)]
        action['context'] = {'default_catalog_id': self.id}
        return action

    # -------------------------------------------------------------------------
    # Motor de Ejecución de Chunking (Asíncrono con Estado en Tiempo Real / Síncrono)
    # -------------------------------------------------------------------------
    def _trigger_chunking(self, async_mode: bool = True, resume: bool = False, from_startup: bool = False):
        self.ensure_one()
        if self.env.context.get('skip_auto_index'):
            return

        if not async_mode:
            self._run_chunking_pipeline(resume=resume)
            return

        catalog_id = self.id
        with _WORKER_LOCK:
            _CANCEL_CATALOG_WORKERS.discard(catalog_id)
            if catalog_id in _ACTIVE_CATALOG_WORKERS:
                _logger.info("[product_catalog] El catálogo ID %s ya tiene un worker activo en ejecución.", catalog_id)
                return
            _ACTIVE_CATALOG_WORKERS.add(catalog_id)

        if not from_startup:
            self.with_context(skip_auto_index=True).write({
                'index_state': 'processing',
                'index_log': _('Iniciando/reanudando extracción de páginas y embeddings en segundo plano...'),
            })

        db_name = self.env.cr.dbname
        uid = self.env.uid
        context_copy = dict(self.env.context, skip_auto_index=True)

        def _background_worker():
            try:
                registry = odoo.modules.registry.Registry(db_name)
                with registry.cursor() as new_cr:
                    new_cr.execute("SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL READ COMMITTED")
                    new_cr.commit()
                    env = api.Environment(new_cr, uid, context_copy)
                    cat = env['product.catalog'].browse(catalog_id)
                    if cat.exists():
                        cat._run_chunking_pipeline(commit_each_page=True, resume=True)
            except Exception as bg_err:
                _logger.exception("Error en worker de segundo plano para product.catalog %s: %s", catalog_id, bg_err)
            finally:
                with _WORKER_LOCK:
                    _ACTIVE_CATALOG_WORKERS.discard(catalog_id)

        def _launch_worker_thread():
            worker_thread = threading.Thread(
                target=_background_worker,
                name=f"odoo-catalog-chunker-{catalog_id}",
                daemon=True,
            )
            worker_thread.start()

        if not from_startup and not odoo.tools.config.get('test_enable') and not self.env.context.get('test_enable'):
            self.env.cr.postcommit.add(_launch_worker_thread)
        else:
            _launch_worker_thread()

    def _upsert_page_chunk(
        self,
        page_num: int,
        total_pages: int,
        chunk_type: str,
        target_dim: int,
        model_name: str,
        raw_text: str,
        source_text: str,
        img_b64: str,
        image_filename: str,
        meta_dict: dict,
        content_hash: str,
        vector_values: list[float],
    ):
        """
        Reemplaza o crea (Upsert) el registro en `product.catalog.embedding` para la llave lógica
        `(catalog_id, page_number, chunk_type)`, asegurando que nunca se dupliquen registros
        para una misma página y modalidad.
        """
        EmbeddingModel = self.env['product.catalog.embedding']
        existing = EmbeddingModel.search([
            ('catalog_id', '=', self.id),
            ('page_number', '=', page_num),
            ('chunk_type', '=', chunk_type),
        ], limit=1)

        vals = {
            'catalog_id': self.id,
            'page_number': page_num,
            'total_pages': total_pages,
            'chunk_type': chunk_type,
            'ai_provider': self.ai_provider,
            'ai_model': model_name,
            'dimension': target_dim,
            'raw_page_text': raw_text,
            'source_text': source_text,
            'page_image': img_b64,
            'page_image_filename': image_filename,
            'metadata_json': json.dumps(meta_dict, ensure_ascii=False, indent=2),
            'char_count': meta_dict.get('char_count', 0),
            'word_count': meta_dict.get('word_count', 0),
            'embedded_image_count': meta_dict.get('embedded_image_count', 0),
            'content_hash': content_hash,
        }
        if existing:
            existing.write(vals)
            rec = existing
        else:
            rec = EmbeddingModel.create(vals)

        rec.set_vector_embedding(vector_values, dim=target_dim)
        return rec

    def _run_chunking_pipeline(self, commit_each_page: bool = False, resume: bool = True):
        """
        Ejecuta paso a paso en streaming el chunking por página del PDF y realiza un Upsert
        por `(catalog_id, page_number, chunk_type)` en `product.catalog.embedding`.
        Reutiliza los chunks cuyo hash/modelo/dimensión no haya cambiado.
        """
        self.ensure_one()
        log_lines = [line for line in (self.index_log or '').splitlines() if line.strip()][-30:] if resume else []
        use_test_fast_mode = bool(self.env.context.get('use_deterministic_embeddings'))

        try:
            pdf_bytes = base64.b64decode(self.pdf_file)
            dpi = max(72, min(int(self.render_dpi or 120), 200))
            total_pages = get_pdf_total_pages(pdf_bytes)
            target_dim = int(self.target_dimension or 768)
            model_name = (self.ai_model or DEFAULT_HF_MODEL).strip()
            required_types = {'text', 'image'} if self.embedding_mode == 'both' else {self.embedding_mode}

            # Si se cambió a un modo que no requiere uno de los tipos (ej. de 'both' a 'text'), limpiar tipos sobrantes
            obsolete_chunks = self.embedding_ids.filtered(lambda e: e.chunk_type not in required_types)
            if obsolete_chunks:
                obsolete_chunks.unlink()

            # Determinar qué páginas ya tienen TODOS sus chunks requeridos con el mismo modelo y dimensión
            fully_done_pages = set()
            if resume and self.embedding_ids:
                by_page = {}
                for emb in self.embedding_ids:
                    if emb.ai_model == model_name and emb.dimension == target_dim and emb.vector_preview:
                        by_page.setdefault(emb.page_number, set()).add(emb.chunk_type)
                for p_num, done_types in by_page.items():
                    if required_types.issubset(done_types):
                        fully_done_pages.add(p_num)

            log_lines.append(
                f"Iniciando chunking ({self.embedding_mode}, {target_dim}d): {total_pages} páginas totales "
                f"({len(fully_done_pages)} completas previamente)."
            )
            self.with_context(skip_auto_index=True).write({
                'index_state': 'processing',
                'page_count': total_pages,
                'indexed_pages': len(fully_done_pages),
                'index_log': "\n".join(log_lines[-40:]),
            })
            if commit_each_page:
                self.env.cr.commit()

            hf_token = self.env['ir.config_parameter'].sudo().get_param('product_catalog.hf_token', default='')
            tags_names = self.tag_ids.mapped('name')
            partner_name = self.partner_id.name or ''
            catalog_title = self.title or ''
            EmbeddingModel = self.env['product.catalog.embedding']
            warnings_seen = set()

            for page_data in iter_pdf_page_chunks(pdf_bytes, dpi=dpi, skip_pages=fully_done_pages):
                if self.id in _CANCEL_CATALOG_WORKERS:
                    _logger.info("[product_catalog] Catálogo ID %s detenido por solicitud del usuario.", self.id)
                    return

                page_num = page_data['page_number']
                raw_text = page_data['raw_text']
                img_b64 = page_data['page_image_b64']
                pil_img = page_data['page_pil_image']
                page_meta = dict(page_data['page_metadata'])

                page_meta.update({
                    'catalog_id': self.id,
                    'catalog_title': catalog_title,
                    'partner_id': self.partner_id.id,
                    'partner_name': partner_name,
                    'partner_vat': self.partner_id.vat or '',
                    'tags': tags_names,
                    'tag_ids': self.tag_ids.ids,
                    'date_start': fields.Datetime.to_string(self.date_start) if self.date_start else None,
                    'date_end': fields.Datetime.to_string(self.date_end) if self.date_end else None,
                    'target_dimension': target_dim,
                    'requested_model': model_name,
                })

                formatted_prompt = format_gemma_document_prompt(
                    catalog_title=catalog_title,
                    partner_name=partner_name,
                    page_number=page_num,
                    total_pages=total_pages,
                    tags_list=tags_names,
                    raw_page_text=raw_text,
                )
                image_filename = f"catalog_{self.id}_page_{page_num}.png"

                # 1. Chunk de Texto de la Página (Upsert por page_number + chunk_type='text')
                if self.embedding_mode in ('text', 'both'):
                    text_hash = compute_sha256(formatted_prompt)
                    existing_text = EmbeddingModel.search([
                        ('catalog_id', '=', self.id),
                        ('page_number', '=', page_num),
                        ('chunk_type', '=', 'text'),
                    ], limit=1)

                    if (
                        resume
                        and existing_text
                        and existing_text.content_hash == text_hash
                        and existing_text.ai_model == model_name
                        and existing_text.dimension == target_dim
                        and existing_text.vector_preview
                    ):
                        _logger.info("[product_catalog] Catálogo %s Pág. %s: chunk 'text' ya al día, reutilizando.", self.id, page_num)
                    else:
                        text_vec, effective_model, warn_msg = generate_text_embedding(
                            formatted_text=formatted_prompt,
                            target_dim=target_dim,
                            model_name=model_name,
                            hf_token=hf_token,
                            allow_open_fallback=self.allow_open_fallback,
                            use_deterministic_test_mode=use_test_fast_mode,
                        )
                        if warn_msg and warn_msg not in warnings_seen:
                            warnings_seen.add(warn_msg)
                            log_lines.append(warn_msg)

                        text_meta = dict(page_meta, chunk_type='text', effective_model=effective_model)
                        self._upsert_page_chunk(
                            page_num=page_num,
                            total_pages=total_pages,
                            chunk_type='text',
                            target_dim=target_dim,
                            model_name=model_name,
                            raw_text=raw_text,
                            source_text=formatted_prompt,
                            img_b64=img_b64,
                            image_filename=image_filename,
                            meta_dict=text_meta,
                            content_hash=text_hash,
                            vector_values=text_vec,
                        )
                        if commit_each_page:
                            self.env.cr.commit()

                if self.id in _CANCEL_CATALOG_WORKERS:
                    try:
                        pil_img.close()
                    except Exception:
                        pass
                    return

                # 2. Chunk de Imagen de la Página (Upsert por page_number + chunk_type='image')
                if self.embedding_mode in ('image', 'both'):
                    img_hash = compute_sha256(page_data['page_image_png_bytes'])
                    existing_img = EmbeddingModel.search([
                        ('catalog_id', '=', self.id),
                        ('page_number', '=', page_num),
                        ('chunk_type', '=', 'image'),
                    ], limit=1)

                    if (
                        resume
                        and existing_img
                        and existing_img.content_hash == img_hash
                        and existing_img.ai_model == model_name
                        and existing_img.dimension == target_dim
                        and existing_img.vector_preview
                    ):
                        _logger.info("[product_catalog] Catálogo %s Pág. %s: chunk 'image' ya al día, reutilizando.", self.id, page_num)
                    else:
                        img_vec, effective_model_img, warn_msg_img = generate_image_embedding(
                            pil_image=pil_img,
                            fallback_caption_text=formatted_prompt,
                            target_dim=target_dim,
                            model_name=model_name,
                            hf_token=hf_token,
                            allow_open_fallback=self.allow_open_fallback,
                            use_deterministic_test_mode=use_test_fast_mode,
                        )
                        if warn_msg_img and warn_msg_img not in warnings_seen:
                            warnings_seen.add(warn_msg_img)
                            log_lines.append(warn_msg_img)

                        img_meta = dict(page_meta, chunk_type='image', effective_model=effective_model_img)
                        self._upsert_page_chunk(
                            page_num=page_num,
                            total_pages=total_pages,
                            chunk_type='image',
                            target_dim=target_dim,
                            model_name=model_name,
                            raw_text=raw_text,
                            source_text=f"[IMAGEN PÁGINA {page_num}] {formatted_prompt}",
                            img_b64=img_b64,
                            image_filename=image_filename,
                            meta_dict=img_meta,
                            content_hash=img_hash,
                            vector_values=img_vec,
                        )
                        if commit_each_page:
                            self.env.cr.commit()

                try:
                    pil_img.close()
                except Exception:
                    pass

                fully_done_pages.add(page_num)
                done_count = len(fully_done_pages)
                progress_pct = round((done_count / total_pages) * 100.0, 1) if total_pages else 0.0
                msg_line = (
                    f"Página {page_num}/{total_pages} ({progress_pct}%) completada "
                    f"({target_dim}d, modo={self.embedding_mode}, "
                    f"{page_meta.get('word_count', 0)} palabras, {page_meta.get('embedded_image_count', 0)} imgs)."
                )
                log_lines.append(msg_line)
                _logger.info("[product_catalog] Catálogo ID %s (%s): %s", self.id, self.title, msg_line)

                self.with_context(skip_auto_index=True).write({
                    'indexed_pages': done_count,
                    'index_log': "\n".join(log_lines[-40:]),
                })
                if commit_each_page:
                    self.env.cr.commit()

            log_lines.append(
                f"Proceso finalizado exitosamente. Total páginas: {total_pages} | "
                f"Total chunks/embeddings activos: {len(self.embedding_ids)}."
            )
            self.with_context(skip_auto_index=True).write({
                'index_state': 'done',
                'indexed_pages': total_pages,
                'last_indexed_at': fields.Datetime.now(),
                'index_log': "\n".join(log_lines[-40:]),
            })
            if commit_each_page:
                self.env.cr.commit()

        except Exception as err:
            tb_str = traceback.format_exc()
            _logger.error("Error procesando catálogo PDF %s: %s", self.id, err)
            log_lines.append(f"ERROR durante el chunking/vectorización: {err}")
            log_lines.append(tb_str)
            if commit_each_page:
                try:
                    self.env.cr.rollback()
                except Exception:
                    pass
            self.with_context(skip_auto_index=True).write({
                'index_state': 'error',
                'index_log': "\n".join(log_lines[-40:]),
            })
            if commit_each_page:
                self.env.cr.commit()
