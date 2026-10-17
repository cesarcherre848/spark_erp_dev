# -*- coding: utf-8 -*-
import base64
import json
import logging
from datetime import datetime, timezone

from odoo import http, fields
from odoo.http import request

from ..services.embedding_service import (
    DEFAULT_HF_MODEL,
    format_gemma_query_prompt,
    generate_text_embedding,
)

_logger = logging.getLogger(__name__)


def _parse_datetime(val):
    if not val:
        return None
    val = str(val).strip()
    try:
        clean_val = val.replace('Z', '+00:00')
        dt = datetime.fromisoformat(clean_val)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        pass

    try:
        return fields.Datetime.to_datetime(val)
    except Exception:
        pass

    raise ValueError(
        f"Formato de fecha inválido: '{val}'. Utilice formato ISO (ej. YYYY-MM-DDTHH:MM:SS o YYYY-MM-DD HH:MM:SS)"
    )


def _json_response(data, status=200):
    return request.make_response(
        json.dumps(data, default=str),
        headers=[('Content-Type', 'application/json; charset=utf-8')],
        status=status,
    )


def _error_response(message, status=400, details=None):
    payload = {
        'status': 'error',
        'code': status,
        'message': message,
    }
    if details:
        payload['details'] = details
    return _json_response(payload, status=status)


class ProductCatalogApiController(http.Controller):

    @http.route(
        ['/api/v1/product_catalog/search', '/api/v1/product-catalog/search'],
        type='http',
        auth='public',
        methods=['GET', 'POST'],
        csrf=False,
    )
    def search_catalog_pages(self, **kwargs):
        """
        API REST para buscar páginas (chunks) de catálogos de proveedores por similitud vectorial
        con google/embeddinggemma-2 y/o filtros de proveedor, etiquetas y vigencia temporal.
        Devuelve tanto el texto de la página como la URL directa (y opcionalmente Base64)
        de la imagen capturada de la página.
        """
        params = dict(kwargs)
        if request.httprequest.method == 'POST' and request.httprequest.data:
            try:
                body_params = json.loads(request.httprequest.data.decode('utf-8'))
                if isinstance(body_params, dict):
                    params.update(body_params)
            except Exception as exc:
                return _error_response(f"Cuerpo JSON no válido: {exc}", status=400)

        # 1. Filtro por Proveedor (partner_id, supplier_id o vat)
        partner_id = params.get('supplier_id') or params.get('partner_id')
        vat = params.get('vat')
        partner_obj = None

        if partner_id:
            try:
                partner_id = int(partner_id)
            except (ValueError, TypeError):
                return _error_response("El parámetro 'partner_id' (o 'supplier_id') debe ser un entero.", status=400)
            partner_obj = request.env['res.partner'].sudo().browse(partner_id)
            if not partner_obj.exists():
                return _error_response(f"No se encontró el proveedor con ID: {partner_id}", status=404)
        elif vat:
            partner_obj = request.env['res.partner'].sudo().search([('vat', '=', vat)], limit=1)
            if not partner_obj:
                return _error_response(f"No se encontró ningún proveedor con VAT: '{vat}'", status=404)
            partner_id = partner_obj.id

        # 2. Filtro por Catálogo específico
        catalog_id = params.get('catalog_id')
        if catalog_id:
            try:
                catalog_id = int(catalog_id)
            except (ValueError, TypeError):
                return _error_response("El parámetro 'catalog_id' debe ser un entero.", status=400)

        # 3. Filtro por Vigencia Temporal
        validity_date_raw = params.get('validity_date') or params.get('date')
        only_active = str(params.get('only_active', 'true')).lower() in ('true', '1', 'yes')
        validity_dt = None
        if validity_date_raw:
            try:
                validity_dt = _parse_datetime(validity_date_raw)
            except ValueError as ve:
                return _error_response(str(ve), status=400)
        elif only_active:
            validity_dt = fields.Datetime.now()

        # 4. Filtro por Etiquetas (tag_ids o tag)
        tag_param = params.get('tag_ids') or params.get('tag_id')
        tag_name = params.get('tag')
        tag_ids_filter = []
        if tag_param:
            if isinstance(tag_param, (list, tuple)):
                tag_ids_filter = [int(x) for x in tag_param if str(x).isdigit()]
            elif isinstance(tag_param, str):
                tag_ids_filter = [int(x.strip()) for x in tag_param.split(',') if x.strip().isdigit()]
            elif isinstance(tag_param, int):
                tag_ids_filter = [tag_param]
        elif tag_name:
            matched_tags = request.env['product.tag'].sudo().search([('name', 'ilike', str(tag_name).strip())])
            tag_ids_filter = matched_tags.ids

        # 5. Parámetros de Búsqueda Semántica (query, dimension, model, chunk_type)
        query_text = (params.get('query') or params.get('q') or '').strip()
        chunk_type = params.get('chunk_type')
        include_image_b64 = str(params.get('include_image', 'false')).lower() in ('true', '1', 'yes')
        use_deterministic = str(params.get('deterministic_test', 'false')).lower() in ('true', '1', 'yes')

        try:
            dimension = int(params.get('dimension', 768))
            limit = min(int(params.get('limit', 10)), 100)
        except (ValueError, TypeError):
            dimension = 768
            limit = 10

        model_name = (params.get('ai_model') or DEFAULT_HF_MODEL).strip()
        query_vector = None
        if query_text:
            try:
                hf_token = request.env['ir.config_parameter'].sudo().get_param('product_catalog.hf_token', default='')
                formatted_q = format_gemma_query_prompt(query_text)
                query_vector, _, _ = generate_text_embedding(
                    formatted_text=formatted_q,
                    target_dim=dimension,
                    model_name=model_name,
                    hf_token=hf_token,
                    allow_open_fallback=True,
                    use_deterministic_test_mode=use_deterministic,
                )
            except Exception as emb_err:
                return _error_response(f"Error vectorizando consulta '{query_text}': {emb_err}", status=500)

        # 6. Ejecutar búsqueda vectorial + relacional en pgvector
        EmbeddingModel = request.env['product.catalog.embedding'].sudo()
        try:
            rows = EmbeddingModel.search_similar_chunks(
                query_vector=query_vector,
                dimension=dimension,
                partner_id=partner_id,
                tag_ids=tag_ids_filter if tag_ids_filter else None,
                catalog_id=catalog_id,
                chunk_type=chunk_type,
                validity_dt=validity_dt,
                limit=limit,
            )
        except Exception as search_err:
            return _error_response(f"Error en búsqueda vectorial: {search_err}", status=400)

        # Enriquecer resultados con URL de imagen de página y (opcionalmente) Base64
        results = []
        for r in rows:
            emb_id = r['embedding_id']
            cat_id = r['catalog_id']
            page_num = r['page_number']
            emb_rec = EmbeddingModel.browse(emb_id)
            cat_rec = emb_rec.catalog_id

            item = {
                'embedding_id': emb_id,
                'catalog_id': cat_id,
                'catalog_title': cat_rec.title,
                'partner_id': cat_rec.partner_id.id,
                'partner_name': cat_rec.partner_id.name,
                'partner_vat': cat_rec.partner_id.vat or '',
                'page_number': page_num,
                'total_pages': r.get('total_pages') or cat_rec.page_count,
                'chunk_type': r['chunk_type'],
                'ai_model': r['ai_model'],
                'dimension': r['dimension'],
                'similarity': round(float(r.get('similarity') or 0.0), 6),
                'raw_page_text': r.get('raw_page_text') or '',
                'source_text': r.get('source_text') or '',
                'tags': [{'id': t.id, 'name': t.name, 'color': t.color or ''} for t in cat_rec.tag_ids],
                'date_start': fields.Datetime.to_string(cat_rec.date_start) if cat_rec.date_start else None,
                'date_end': fields.Datetime.to_string(cat_rec.date_end) if cat_rec.date_end else None,
                'page_image_url': f"/api/v1/product_catalog/{cat_id}/page/{page_num}/image",
                'chunk_image_url': f"/api/v1/product_catalog/chunk/{emb_id}/image",
                'metadata': r.get('metadata') or {},
            }
            if include_image_b64 and emb_rec.page_image:
                item['page_image_base64'] = (
                    emb_rec.page_image.decode('ascii')
                    if isinstance(emb_rec.page_image, bytes)
                    else str(emb_rec.page_image)
                )
            results.append(item)

        return _json_response({
            'status': 'success',
            'count': len(results),
            'query': query_text or None,
            'dimension': dimension,
            'data': results,
        }, status=200)

    @http.route(
        '/api/v1/product_catalog/chunk/<int:embedding_id>/image',
        type='http',
        auth='public',
        methods=['GET'],
        csrf=False,
    )
    def get_chunk_page_image(self, embedding_id, **kwargs):
        """
        Devuelve directamente la imagen PNG capturada para un chunk específico.
        """
        emb = request.env['product.catalog.embedding'].sudo().browse(embedding_id)
        if not emb.exists() or not emb.page_image:
            return _error_response(f"No se encontró imagen para el chunk ID: {embedding_id}", status=404)

        image_bytes = base64.b64decode(emb.page_image)
        filename = emb.page_image_filename or f"catalog_{emb.catalog_id.id}_page_{emb.page_number}.png"
        return request.make_response(
            image_bytes,
            headers=[
                ('Content-Type', 'image/png'),
                ('Content-Disposition', f'inline; filename="{filename}"'),
                ('Cache-Control', 'public, max-age=3600'),
            ],
        )

    @http.route(
        '/api/v1/product_catalog/<int:catalog_id>/page/<int:page_number>/image',
        type='http',
        auth='public',
        methods=['GET'],
        csrf=False,
    )
    def get_catalog_page_image(self, catalog_id, page_number, **kwargs):
        """
        Devuelve directamente la imagen PNG capturada de la página `page_number` del catálogo `catalog_id`.
        """
        emb = request.env['product.catalog.embedding'].sudo().search([
            ('catalog_id', '=', catalog_id),
            ('page_number', '=', page_number),
            ('page_image', '!=', False),
        ], limit=1)

        if not emb:
            return _error_response(
                f"No se encontró imagen para el catálogo {catalog_id}, página {page_number}.",
                status=404,
            )

        image_bytes = base64.b64decode(emb.page_image)
        filename = emb.page_image_filename or f"catalog_{catalog_id}_page_{page_number}.png"
        return request.make_response(
            image_bytes,
            headers=[
                ('Content-Type', 'image/png'),
                ('Content-Disposition', f'inline; filename="{filename}"'),
                ('Cache-Control', 'public, max-age=3600'),
            ],
        )
