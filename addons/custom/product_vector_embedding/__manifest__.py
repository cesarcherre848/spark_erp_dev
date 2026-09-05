# -*- coding: utf-8 -*-
{
    'name': 'Product Vector Embedding Metadata',
    'version': '1.0.0',
    'category': 'Technical/AI',
    'summary': 'Esquema vectorial HNSW y visor de metadatos de embeddings de productos',
    'description': """
Product Vector Embedding
========================
Módulo que define el esquema relacional para almacenar embeddings vectoriales
de productos generados por múltiples modelos de IA (OpenAI, Gemini, Ollama, etc.).

Características:
- Claves foráneas vinculadas a product.product (SKU) y product.template con ON DELETE CASCADE.
- Soporte para 4 dimensiones vectoriales estándar (1536, 1024, 768, 384).
- Creación automática de la extensión pgvector e índices HNSW parciales con métrica Coseno (vector_cosine_ops).
- Almacenamiento de metadatos de control (ai_provider, ai_model, dimension, source_text, content_hash SHA-256).
- Vistas en Odoo para auditoría e inspección de metadatos.
- Diseñado para integración directa de alta velocidad con servicios externos de IA vía PostgreSQL.
    """,
    'author': 'Spark ERP',
    'depends': ['base', 'product'],
    'data': [
        'security/ir.model.access.csv',
        'views/product_vector_embedding_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
