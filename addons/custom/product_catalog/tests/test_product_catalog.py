import base64
from datetime import timedelta
import pymupdf as fitz

from odoo import fields
from odoo.tests.common import TransactionCase
from odoo.exceptions import ValidationError


class TestProductCatalog(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env['res.partner'].create({
            'name': 'Laboratorios Cosméticos Andinos S.A.',
            'email': 'catalogos@cosmeticosandinos.com',
            'vat': '20555666777',
            'supplier_rank': 1,
        })
        cls.now = fields.Datetime.now()
        cls.tag_c10 = cls.env['product.tag'].create({
            'name': 'Campaña C10',
            'color': '1',
        })
        cls.tag_facial = cls.env['product.tag'].create({
            'name': 'Línea Facial',
            'color': '4',
        })
        cls.sample_pdf_b64 = cls._generate_sample_3page_pdf_b64()

    @classmethod
    def _generate_sample_3page_pdf_b64(cls) -> bytes:
        """Genera un PDF real de 3 páginas en memoria usando PyMuPDF (fitz)."""
        doc = fitz.open()
        pages_content = [
            (
                "FAMILIA 1: CUIDADO FACIAL ANTI-EDAD\n"
                "SKU-FAC-001: Crema Hidratante Ácido Hialurónico 50ml - Precio S/ 89.90\n"
                "SKU-FAC-002: Sérum Vitamina C Concentrado 30ml - Precio S/ 115.00"
            ),
            (
                "FAMILIA 2: FRAGANCIAS Y PERFUMERÍA FINA\n"
                "SKU-PER-101: Eau de Parfum Floral Intense 100ml - Precio S/ 160.00\n"
                "SKU-PER-102: Colonia Refrescante Citrus Verbena 150ml - Precio S/ 75.00"
            ),
            (
                "FAMILIA 3: PROTECCIÓN SOLAR DERMATOLÓGICA\n"
                "SKU-SOL-201: Bloqueador Solar FPS 50+ Toque Seco 60ml - Precio S/ 68.00\n"
                "SKU-SOL-202: Gel Post Solar Aloe Vera Puro 120ml - Precio S/ 42.00"
            ),
        ]
        for idx, text in enumerate(pages_content):
            page = doc.new_page(width=595, height=842)
            # Dibujar un rectángulo de color para simular diseño gráfico del catálogo
            rect = fitz.Rect(40, 40, 555, 140)
            page.draw_rect(rect, color=(0.1, 0.3, 0.6), fill=(0.9, 0.95, 1.0))
            page.insert_text((55, 80), f"CATÁLOGO OFICIAL - PÁGINA {idx + 1}", fontsize=16)
            page.insert_textbox(fitz.Rect(50, 170, 545, 750), text, fontsize=12)

        pdf_bytes = doc.tobytes()
        doc.close()
        return base64.b64encode(pdf_bytes)

    def test_01_auto_chunking_on_upload_and_page_images(self):
        """
        Verifica que al subir un PDF de 3 páginas con auto_index=True:
        - El estado pase a 'done' (Finalizado) con 100% de progreso.
        - Se creen los chunks de texto e imagen por página (6 registros para 3 páginas en modo 'both').
        - Cada chunk tenga la imagen PNG capturada de la página y el vector 768d en pgvector.
        """
        catalog = self.env['product.catalog'].with_context(
            test_enable=True,
            use_deterministic_embeddings=True,
        ).create({
            'title': 'Catálogo Campaña C10 Oficial',
            'partner_id': self.partner.id,
            'pdf_file': self.sample_pdf_b64,
            'pdf_filename': 'catalogo_c10.pdf',
            'date_start': self.now - timedelta(days=1),
            'date_end': self.now + timedelta(days=30),
            'tag_ids': [(6, 0, [self.tag_c10.id, self.tag_facial.id])],
            'ai_model': 'google/embeddinggemma-2',
            'target_dimension': '768',
            'embedding_mode': 'both',
            'auto_index': True,
        })

        self.assertEqual(catalog.index_state, 'done')
        self.assertEqual(catalog.page_count, 3)
        self.assertEqual(catalog.indexed_pages, 3)
        self.assertEqual(catalog.index_progress, 100.0)
        self.assertEqual(catalog.state, 'active')
        self.assertTrue(catalog.is_valid)
        self.assertEqual(catalog.embedding_count, 6)  # 3 páginas * (texto + imagen)

        # Verificar que cada página tenga su imagen PNG capturada y su vector de 768 dimensiones
        page_1_text = catalog.embedding_ids.filtered(
            lambda e: e.page_number == 1 and e.chunk_type == 'text'
        )
        self.assertEqual(len(page_1_text), 1)
        self.assertIn('SKU-FAC-001', page_1_text.raw_page_text)
        self.assertTrue(page_1_text.page_image)
        raw_png = base64.b64decode(page_1_text.page_image)
        self.assertTrue(raw_png.startswith(b'\x89PNG'))
        self.assertIn(f"/api/v1/product_catalog/{catalog.id}/page/1/image", page_1_text.page_image_url)

        vec_768 = page_1_text.get_vector_embedding(768)
        self.assertEqual(len(vec_768), 768)

    def test_02_manual_chunking_button_and_matryoshka_dimensions(self):
        """
        Verifica el estado 'pending' (No Iniciado) cuando auto_index=False,
        el botón manual action_start_chunking y el cambio de dimensión Matryoshka (256d).
        """
        catalog = self.env['product.catalog'].with_context(
            test_enable=True,
            use_deterministic_embeddings=True,
        ).create({
            'title': 'Catálogo Manual 256d',
            'partner_id': self.partner.id,
            'pdf_file': self.sample_pdf_b64,
            'pdf_filename': 'catalogo_256.pdf',
            'date_start': self.now,
            'target_dimension': '256',
            'embedding_mode': 'text',
            'auto_index': False,
        })

        # Inicialmente no ha empezado el chunking
        self.assertEqual(catalog.index_state, 'pending')
        self.assertEqual(catalog.embedding_count, 0)
        self.assertEqual(catalog.index_progress, 0.0)

        # Activar manualmente desde el botón
        catalog.with_context(
            test_enable=True,
            use_deterministic_embeddings=True,
        ).action_start_chunking()

        self.assertEqual(catalog.index_state, 'done')
        self.assertEqual(catalog.page_count, 3)
        self.assertEqual(catalog.embedding_count, 3)

        first_chunk = catalog.embedding_ids[0]
        self.assertEqual(first_chunk.dimension, 256)
        vec_256 = first_chunk.get_vector_embedding(256)
        self.assertEqual(len(vec_256), 256)

        # Probar reinicio manual del estado
        catalog.action_reset_chunking()
        self.assertEqual(catalog.index_state, 'pending')
        self.assertEqual(catalog.indexed_pages, 0)

    def test_03_validity_states_and_date_constraints(self):
        """Verifica la validación de fechas y estados upcoming, active, expired."""
        with self.assertRaises(ValidationError):
            self.env['product.catalog'].create({
                'title': 'Catálogo Fechas Inválidas',
                'partner_id': self.partner.id,
                'pdf_file': self.sample_pdf_b64,
                'pdf_filename': 'invalido.pdf',
                'date_start': self.now,
                'date_end': self.now - timedelta(days=3),
                'auto_index': False,
            })

        expired_cat = self.env['product.catalog'].create({
            'title': 'Catálogo Vencido',
            'partner_id': self.partner.id,
            'pdf_file': self.sample_pdf_b64,
            'pdf_filename': 'vencido.pdf',
            'date_start': self.now - timedelta(days=20),
            'date_end': self.now - timedelta(days=5),
            'auto_index': False,
        })
        self.assertEqual(expired_cat.state, 'expired')
        self.assertFalse(expired_cat.is_valid)

    def test_04_semantic_search_with_partner_and_tag_filters(self):
        """Verifica la búsqueda vectorial HNSW combinada con filtros de proveedor, etiquetas y vigencia."""
        catalog = self.env['product.catalog'].with_context(
            test_enable=True,
            use_deterministic_embeddings=True,
        ).create({
            'title': 'Catálogo Búsqueda Vectorial',
            'partner_id': self.partner.id,
            'pdf_file': self.sample_pdf_b64,
            'pdf_filename': 'busqueda.pdf',
            'date_start': self.now - timedelta(days=2),
            'date_end': self.now + timedelta(days=10),
            'tag_ids': [(6, 0, [self.tag_facial.id])],
            'target_dimension': '768',
            'embedding_mode': 'text',
            'auto_index': True,
        })

        page_2_chunk = catalog.embedding_ids.filtered(lambda e: e.page_number == 2)
        query_vec = page_2_chunk.get_vector_embedding(768)

        results = self.env['product.catalog.embedding'].search_similar_chunks(
            query_vector=query_vec,
            dimension=768,
            partner_id=self.partner.id,
            tag_ids=[self.tag_facial.id],
            catalog_id=catalog.id,
            chunk_type='text',
            validity_dt=self.now,
            limit=3,
        )
        self.assertEqual(len(results), 3)
        # El primer resultado debe ser exactamente la página 2 (similitud ~1.0)
        self.assertEqual(results[0]['page_number'], 2)
        self.assertAlmostEqual(results[0]['similarity'], 1.0, places=4)
