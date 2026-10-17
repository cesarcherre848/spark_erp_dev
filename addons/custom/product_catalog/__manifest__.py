# -*- coding: utf-8 -*-
{
    'name': 'Product Catalog & Page Vector Embeddings',
    'version': '1.0.0',
    'category': 'Sales/Sales',
    'summary': 'Gestión de catálogos PDF por proveedor con vigencia, etiquetas, chunking por página y embeddings locales (EmbeddingGemma-2)',
    'description': """
Product Catalog & Page Vector Embeddings
========================================
Módulo para la gestión de catálogos de productos en PDF asociados a proveedores (res.partner),
con control de vigencia temporal, etiquetas de producto (product.tag), captura de imagen por página
y generación automática/manual de embeddings vectoriales en PostgreSQL (pgvector HNSW).

Características principales:
- Clave foránea a proveedor (res.partner) con integración en la ficha de proveedor (Smart Button y pestaña).
- Carga y visor integrado de documentos PDF (pdf_viewer).
- Control de vigencia temporal (date_start, date_end, estados Por Iniciar / Vigente / Vencido).
- Filtrado por etiquetas de producto (product.tag).
- Estado de chunking visible en tiempo real (No Iniciado, En Curso, Finalizado, Error) con barra de progreso y botón de activación manual.
- Chunking por página (extracción de texto estructurado + captura de imagen PNG de cada página con PyMuPDF).
- Tabla dedicada product.catalog.embedding con soporte multi-dimensión (1536, 1024, 768, 512, 384, 256, 128) e índices HNSW (vector_cosine_ops).
- Integración con modelos locales de Hugging Face (por defecto google/embeddinggemma-2 con soporte Matryoshka).
- API REST (/api/v1/product_catalog/search y /api/v1/product_catalog/<id>/page/<num>/image) para búsqueda semántica y recuperación de la imagen de cada página.
    """,
    'author': 'Spark ERP',
    'depends': ['base', 'product', 'sale'],
    'external_dependencies': {
        'python': ['pymupdf', 'PIL', 'torch', 'transformers', 'sentence_transformers'],
    },
    'data': [
        'security/ir.model.access.csv',
        'views/product_catalog_embedding_views.xml',
        'views/product_catalog_views.xml',
        'views/res_partner_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'product_catalog/static/src/js/catalog_auto_refresh.js',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
