# -*- coding: utf-8 -*-
import hashlib
import logging
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

SUPPORTED_DIMENSIONS = [1536, 1024, 768, 384]


class ProductVectorEmbedding(models.Model):
    _name = 'product.vector.embedding'
    _description = 'Metadatos de Embeddings de Producto'
    _order = 'write_date desc, id desc'

    name = fields.Char(
        string='Referencia',
        compute='_compute_name',
        store=True
    )
    product_tmpl_id = fields.Many2one(
        'product.template',
        string='Plantilla de Producto',
        ondelete='cascade',
        index=True
    )
    product_id = fields.Many2one(
        'product.product',
        string='Variante (SKU)',
        ondelete='cascade',
        index=True
    )
    ai_provider = fields.Selection([
        ('openai', 'OpenAI'),
        ('gemini', 'Google Gemini'),
        ('cohere', 'Cohere'),
        ('ollama', 'Ollama'),
        ('huggingface', 'HuggingFace / Local'),
        ('other', 'Otro')
    ], string='Proveedor IA', default='openai', index=True)

    ai_model = fields.Char(
        string='Modelo IA',
        required=True,
        index=True
    )
    dimension = fields.Integer(
        string='Dimensión',
        required=True
    )
    source_text = fields.Text(
        string='Texto Origen Vectorizado'
    )
    content_hash = fields.Char(
        string='Hash SHA-256',
        size=64,
        index=True
    )
    active = fields.Boolean(
        string='Activo',
        default=True
    )

    @api.depends('product_id.default_code', 'product_id.display_name', 'product_tmpl_id.name', 'ai_model')
    def _compute_name(self):
        for rec in self:
            prod_label = (
                rec.product_id.default_code or rec.product_id.display_name
                if rec.product_id
                else (rec.product_tmpl_id.name if rec.product_tmpl_id else 'Sin Producto')
            )
            rec.name = f"[{rec.ai_model or 'AI'}] {prod_label}"

    @api.model
    def compute_content_hash(self, text: str) -> str:
        """Calcula el hash SHA-256 de una cadena de texto para control de cambios."""
        if not text:
            return ""
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    def init(self):
        """
        Hook ejecutado al instalar o actualizar el módulo en Odoo.
        Garantiza la presencia de la extensión pgvector, las columnas vectoriales
        y los índices HNSW con métrica Coseno en PostgreSQL.
        """
        super().init()
        cr = self.env.cr

        # 1. Asegurar extensión pgvector
        cr.execute("CREATE EXTENSION IF NOT EXISTS vector;")

        # 2. Asegurar columnas vectoriales e índices HNSW parciales
        for dim in SUPPORTED_DIMENSIONS:
            col_name = f"embedding_{dim}"
            idx_name = f"product_vector_embedding_hnsw_{dim}_idx"

            # Columna tipo vector(dim)
            cr.execute(f"""
                ALTER TABLE product_vector_embedding 
                ADD COLUMN IF NOT EXISTS {col_name} vector({dim});
            """)

            # Índice HNSW parcial con distancia Coseno (<=> vector_cosine_ops)
            cr.execute(f"""
                CREATE INDEX IF NOT EXISTS {idx_name}
                ON product_vector_embedding 
                USING hnsw ({col_name} vector_cosine_ops)
                WHERE {col_name} IS NOT NULL;
            """)

        _logger.info("Esquema vectorial e índices HNSW verificados en product_vector_embedding.")
