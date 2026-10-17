# -*- coding: utf-8 -*-
import hashlib
import json
import logging
from odoo import models, fields, api, _
from odoo.exceptions import ValidationError

from ..services.embedding_service import SUPPORTED_DIMENSIONS

_logger = logging.getLogger(__name__)


class ProductCatalogEmbedding(models.Model):
    _name = 'product.catalog.embedding'
    _description = 'Chunk y Embedding por Página de Catálogo'
    _order = 'catalog_id desc, page_number asc, chunk_type asc, id desc'

    name = fields.Char(
        string='Referencia Chunk',
        compute='_compute_name',
        store=True,
        index=True,
    )
    catalog_id = fields.Many2one(
        'product.catalog',
        string='Catálogo',
        required=True,
        ondelete='cascade',
        index=True,
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Proveedor',
        related='catalog_id.partner_id',
        store=True,
        index=True,
    )
    tag_ids = fields.Many2many(
        'product.tag',
        string='Etiquetas de Catálogo',
        related='catalog_id.tag_ids',
    )
    catalog_state = fields.Selection(
        related='catalog_id.state',
        string='Vigencia del Catálogo',
    )
    date_start = fields.Datetime(
        related='catalog_id.date_start',
        string='Inicio Vigencia',
        store=True,
        index=True,
    )
    date_end = fields.Datetime(
        related='catalog_id.date_end',
        string='Fin Vigencia',
        store=True,
        index=True,
    )
    page_number = fields.Integer(
        string='N° de Página',
        required=True,
        index=True,
    )
    total_pages = fields.Integer(
        string='Total Páginas',
    )
    chunk_type = fields.Selection([
        ('text', 'Texto de Página'),
        ('image', 'Imagen de Página'),
    ], string='Modalidad del Chunk', required=True, default='text', index=True)

    ai_provider = fields.Selection([
        ('huggingface', 'HuggingFace / Local'),
        ('gemini', 'Google Gemini'),
        ('openai', 'OpenAI'),
        ('cohere', 'Cohere'),
        ('ollama', 'Ollama'),
        ('other', 'Otro'),
    ], string='Proveedor IA', default='huggingface', required=True, index=True)

    ai_model = fields.Char(
        string='Modelo IA',
        required=True,
        default='google/embeddinggemma-2',
        index=True,
    )
    dimension = fields.Integer(
        string='Dimensión Vectorial',
        required=True,
        default=768,
        index=True,
    )
    raw_page_text = fields.Text(
        string='Texto Extraído de la Página',
    )
    source_text = fields.Text(
        string='Prompt / Texto Vectorizado',
    )
    page_image = fields.Binary(
        string='Imagen Capturada de la Página (PNG)',
        attachment=True,
    )
    page_image_filename = fields.Char(
        string='Archivo de Imagen',
    )
    metadata_json = fields.Text(
        string='Metadatos del Chunk (JSON)',
    )
    char_count = fields.Integer(
        string='Caracteres',
    )
    word_count = fields.Integer(
        string='Palabras',
    )
    embedded_image_count = fields.Integer(
        string='Imágenes en Página',
    )
    content_hash = fields.Char(
        string='Hash SHA-256',
        size=64,
        index=True,
    )
    vector_preview = fields.Char(
        string='Muestra del Vector',
        readonly=True,
        help='Primeras componentes del vector almacenado en pgvector.',
    )
    page_image_url = fields.Char(
        string='URL API de Imagen',
        compute='_compute_image_urls',
    )
    chunk_image_url = fields.Char(
        string='URL API del Chunk',
        compute='_compute_image_urls',
    )
    active = fields.Boolean(
        string='Activo',
        default=True,
    )

    @api.depends('catalog_id.title', 'page_number', 'chunk_type', 'ai_model', 'dimension')
    def _compute_name(self):
        for rec in self:
            cat_title = rec.catalog_id.title if rec.catalog_id else _('Catálogo')
            mode_label = _('Texto') if rec.chunk_type == 'text' else _('Imagen')
            rec.name = f"{cat_title} - Pág. {rec.page_number or 0} [{mode_label} | {rec.dimension or 768}d]"

    @api.depends('catalog_id', 'page_number')
    def _compute_image_urls(self):
        for rec in self:
            if rec.id and rec.catalog_id:
                rec.page_image_url = f"/api/v1/product_catalog/{rec.catalog_id.id}/page/{rec.page_number}/image"
                rec.chunk_image_url = f"/api/v1/product_catalog/chunk/{rec.id}/image"
            else:
                rec.page_image_url = False
                rec.chunk_image_url = False

    @api.model
    def compute_content_hash(self, text: str) -> str:
        if not text:
            return ""
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    def init(self):
        """
        Garantiza que la extensión pgvector, las columnas vectoriales para todos
        los tamaños soportados (1536, 1024, 768, 512, 384, 256, 128) y sus índices
        HNSW con distancia Coseno existan en PostgreSQL.
        """
        super().init()
        cr = self.env.cr

        cr.execute("CREATE EXTENSION IF NOT EXISTS vector;")

        for dim in SUPPORTED_DIMENSIONS:
            col_name = f"embedding_{dim}"
            idx_name = f"product_catalog_embedding_hnsw_{dim}_idx"

            cr.execute(f"""
                ALTER TABLE product_catalog_embedding
                ADD COLUMN IF NOT EXISTS {col_name} vector({dim});
            """)

            cr.execute(f"""
                CREATE INDEX IF NOT EXISTS {idx_name}
                ON product_catalog_embedding
                USING hnsw ({col_name} vector_cosine_ops)
                WHERE {col_name} IS NOT NULL;
            """)

        _logger.info("Esquema vectorial pgvector e índices HNSW verificados en product_catalog_embedding.")

    def set_vector_embedding(self, vector_values: list[float], dim: int = None):
        """
        Guarda el vector numérico en la columna pgvector `embedding_<dim>` correspondiente
        y actualiza `vector_preview`.
        """
        self.ensure_one()
        self.env.flush_all()
        target_dim = int(dim or self.dimension or 768)
        if target_dim not in SUPPORTED_DIMENSIONS:
            raise ValidationError(_("Dimensión vectorial no soportada: %s. Soportadas: %s") % (target_dim, SUPPORTED_DIMENSIONS))

        if len(vector_values) != target_dim:
            raise ValidationError(
                _("La longitud del vector (%s) no coincide con la dimensión objetivo (%s).")
                % (len(vector_values), target_dim)
            )

        col_name = f"embedding_{target_dim}"
        vec_literal = "[" + ",".join(f"{float(v):.8f}" for v in vector_values) + "]"
        preview_str = "[" + ", ".join(f"{float(v):.4f}" for v in vector_values[:6]) + ", ...]"

        # Limpiar otras columnas vectoriales si el registro cambió de dimensión
        clear_clauses = [f"embedding_{d} = NULL" for d in SUPPORTED_DIMENSIONS if d != target_dim]
        set_sql = f"{col_name} = %s::vector, vector_preview = %s, dimension = %s"
        if clear_clauses:
            set_sql += ", " + ", ".join(clear_clauses)

        self.env.cr.execute(
            f"UPDATE product_catalog_embedding SET {set_sql} WHERE id = %s",
            (vec_literal, preview_str, target_dim, self.id),
        )
        self.invalidate_recordset(['vector_preview', 'dimension'])

    def get_vector_embedding(self, dim: int = None) -> list[float]:
        """Recupera la lista de floats almacenada en la columna `embedding_<dim>`."""
        self.ensure_one()
        self.env.flush_all()
        target_dim = int(dim or self.dimension or 768)
        if target_dim not in SUPPORTED_DIMENSIONS:
            return []
        col_name = f"embedding_{target_dim}"
        self.env.cr.execute(
            f"SELECT {col_name}::text FROM product_catalog_embedding WHERE id = %s",
            (self.id,),
        )
        row = self.env.cr.fetchone()
        if not row or not row[0]:
            return []
        raw_str = row[0].strip("[]")
        if not raw_str:
            return []
        return [float(x) for x in raw_str.split(",")]

    @api.model
    def search_similar_chunks(
        self,
        query_vector: list[float] = None,
        dimension: int = 768,
        partner_id: int = None,
        tag_ids: list[int] = None,
        catalog_id: int = None,
        chunk_type: str = None,
        validity_dt=None,
        limit: int = 10,
    ) -> list[dict]:
        """
        Ejecuta una búsqueda vectorial HNSW por similitud coseno en PostgreSQL
        combinada con filtros relacionales (proveedor, etiquetas, catálogo y fecha de vigencia).
        """
        self.env.flush_all()
        dim = int(dimension or 768)
        if dim not in SUPPORTED_DIMENSIONS:
            raise ValidationError(_("Dimensión no soportada: %s") % dim)

        col_name = f"embedding_{dim}"
        where_clauses = ["e.active = TRUE", "c.active = TRUE"]
        params = []

        if query_vector:
            where_clauses.append(f"e.{col_name} IS NOT NULL")

        if partner_id:
            where_clauses.append("e.partner_id = %s")
            params.append(int(partner_id))

        if catalog_id:
            where_clauses.append("e.catalog_id = %s")
            params.append(int(catalog_id))

        if chunk_type in ('text', 'image'):
            where_clauses.append("e.chunk_type = %s")
            params.append(chunk_type)

        if validity_dt:
            where_clauses.append("c.date_start <= %s")
            params.append(validity_dt)
            where_clauses.append("(c.date_end IS NULL OR c.date_end >= %s)")
            params.append(validity_dt)

        if tag_ids:
            where_clauses.append("""
                EXISTS (
                    SELECT 1 FROM product_catalog_product_tag_rel rel
                    WHERE rel.catalog_id = c.id AND rel.tag_id = ANY(%s)
                )
            """)
            params.append(list(tag_ids))

        where_sql = " AND ".join(where_clauses)

        if query_vector:
            vec_literal = "[" + ",".join(f"{float(v):.8f}" for v in query_vector) + "]"
            select_sim = f"1 - (e.{col_name} <=> %s::vector) AS similarity"
            order_sql = f"e.{col_name} <=> %s::vector ASC, e.page_number ASC"
            sql_params = [vec_literal] + params + [vec_literal, int(limit)]
        else:
            select_sim = "1.0 AS similarity"
            order_sql = "c.date_start DESC, e.page_number ASC"
            sql_params = params + [int(limit)]

        query = f"""
            SELECT
                e.id AS embedding_id,
                e.catalog_id,
                e.partner_id,
                e.page_number,
                e.total_pages,
                e.chunk_type,
                e.ai_provider,
                e.ai_model,
                e.dimension,
                e.raw_page_text,
                e.source_text,
                e.metadata_json,
                e.content_hash,
                {select_sim}
            FROM product_catalog_embedding e
            JOIN product_catalog c ON c.id = e.catalog_id
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT %s
        """
        self.env.cr.execute(query, tuple(sql_params))
        rows = self.env.cr.dictfetchall()
        for r in rows:
            if r.get("metadata_json"):
                try:
                    r["metadata"] = json.loads(r["metadata_json"])
                except Exception:
                    r["metadata"] = {}
            else:
                r["metadata"] = {}
        return rows
