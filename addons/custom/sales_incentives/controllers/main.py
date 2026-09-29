# -*- coding: utf-8 -*-
import json
import logging
from datetime import datetime, timezone

from odoo import http, fields
from odoo.http import request

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

    raise ValueError(f"Formato de fecha inválido: '{val}'. Utilice formato ISO (ej. YYYY-MM-DDTHH:MM:SS o YYYY-MM-DD HH:MM:SS)")


def _json_response(data, status=200):
    return request.make_response(
        json.dumps(data, default=str),
        headers=[('Content-Type', 'application/json; charset=utf-8')],
        status=status
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


class SalesIncentivesApiController(http.Controller):

    @http.route(
        ['/api/v1/sales_incentives', '/api/v1/sales-incentives'],
        type='http',
        auth='public',
        methods=['GET', 'POST'],
        csrf=False
    )
    def sales_incentives_rest(self, **kwargs):
        """
        API REST para consultar planes de incentivos por partner y vigencia temporal.
        Admite parámetros vía query string (GET) o cuerpo JSON (POST).
        """
        params = dict(kwargs)

        # Si viene cuerpo JSON en POST
        if request.httprequest.method == 'POST' and request.httprequest.data:
            try:
                body_params = json.loads(request.httprequest.data.decode('utf-8'))
                if isinstance(body_params, dict):
                    params.update(body_params)
            except Exception as e:
                return _error_response(f"Cuerpo JSON no válido: {str(e)}", status=400)

        # 1. Filtro por proveedor / partner (ID o VAT)
        partner_id = params.get('supplier_id') or params.get('partner_id')
        vat = params.get('vat')
        partner_obj = None

        if partner_id:
            try:
                partner_id = int(partner_id)
            except (ValueError, TypeError):
                return _error_response("El parámetro 'supplier_id' (o 'partner_id') debe ser un número entero válido.", status=400)

            partner_obj = request.env['res.partner'].sudo().browse(partner_id)
            if not partner_obj.exists():
                return _error_response(f"No se encontró ningún proveedor con el ID: {partner_id}", status=404)
        elif vat:
            partner_obj = request.env['res.partner'].sudo().search([('vat', '=', vat)], limit=1)
            if not partner_obj:
                return _error_response(f"No se encontró ningún proveedor con el documento/VAT: '{vat}'", status=404)
            partner_id = partner_obj.id

        # 2. Filtro por vigencia temporal
        validity_date_raw = params.get('validity_date') or params.get('date')
        only_active = str(params.get('only_active', '')).lower() in ('true', '1', 'yes')

        validity_dt = None
        if validity_date_raw:
            try:
                validity_dt = _parse_datetime(validity_date_raw)
            except ValueError as ve:
                return _error_response(str(ve), status=400)
        elif only_active:
            validity_dt = fields.Datetime.now()

        # 3. Construcción del dominio de búsqueda
        domain = [('active', '=', True)]
        if partner_id:
            domain.append(('partner_id', '=', partner_id))

        if validity_dt:
            domain.extend([
                ('date_start', '<=', validity_dt),
                '|',
                ('date_end', '=', False),
                ('date_end', '>=', validity_dt)
            ])

        # 4. Paginación
        try:
            limit = min(int(params.get('limit', 50)), 200)
            offset = max(int(params.get('offset', 0)), 0)
        except (ValueError, TypeError):
            limit = 50
            offset = 0

        # 5. Ejecución de consulta con sudo
        incentive_model = request.env['sale.incentive'].sudo()
        total_count = incentive_model.search_count(domain)
        records = incentive_model.search(domain, limit=limit, offset=offset, order='date_start desc, id desc')

        eval_dt = validity_dt or fields.Datetime.now()
        data = []
        for rec in records:
            data.append({
                'id': rec.id,
                'name': rec.name,
                'title': rec.title,
                'supplier_id': rec.partner_id.id,
                'supplier_name': rec.partner_id.name,
                'partner_id': rec.partner_id.id,
                'partner_name': rec.partner_id.name,
                'partner_vat': rec.partner_id.vat or '',
                'description': rec.description or '',
                'observations': rec.observations or '',
                'benefit_plan': rec.title,
                'notes': rec.observations or '',
                'date_start': fields.Datetime.to_string(rec.date_start),
                'date_end': fields.Datetime.to_string(rec.date_end) if rec.date_end else None,
                'state': rec.state,
                'is_valid': rec.is_valid_at(eval_dt),
            })

        response_payload = {
            'status': 'success',
            'count': len(data),
            'total': total_count,
            'limit': limit,
            'offset': offset,
            'filters': {
                'supplier_id': partner_id,
                'supplier_name': partner_obj.name if partner_obj else None,
                'partner_id': partner_id,
                'partner_name': partner_obj.name if partner_obj else None,
                'validity_date': fields.Datetime.to_string(validity_dt) if validity_dt else None,
                'only_active': only_active,
            },
            'data': data
        }
        return _json_response(response_payload, status=200)

    @http.route(
        '/api/json/sales_incentives',
        type='jsonrpc',
        auth='public',
        methods=['POST']
    )
    def sales_incentives_jsonrpc(self, **kwargs):
        """
        Endpoint nativo JSON-RPC para compatibilidad total con clientes RPC de Odoo.
        """
        partner_id = kwargs.get('supplier_id') or kwargs.get('partner_id')
        validity_date_raw = kwargs.get('validity_date') or kwargs.get('date')
        only_active = bool(kwargs.get('only_active', False))

        domain = [('active', '=', True)]
        if partner_id:
            domain.append(('partner_id', '=', int(partner_id)))

        validity_dt = None
        if validity_date_raw:
            validity_dt = _parse_datetime(validity_date_raw)
        elif only_active:
            validity_dt = fields.Datetime.now()

        if validity_dt:
            domain.extend([
                ('date_start', '<=', validity_dt),
                '|',
                ('date_end', '=', False),
                ('date_end', '>=', validity_dt)
            ])

        records = request.env['sale.incentive'].sudo().search(domain, order='date_start desc, id desc')
        eval_dt = validity_dt or fields.Datetime.now()

        return {
            'status': 'success',
            'count': len(records),
            'data': [{
                'id': rec.id,
                'name': rec.name,
                'title': rec.title,
                'supplier_id': rec.partner_id.id,
                'supplier_name': rec.partner_id.name,
                'partner_id': rec.partner_id.id,
                'partner_name': rec.partner_id.name,
                'description': rec.description or '',
                'observations': rec.observations or '',
                'benefit_plan': rec.title,
                'notes': rec.observations or '',
                'date_start': fields.Datetime.to_string(rec.date_start),
                'date_end': fields.Datetime.to_string(rec.date_end) if rec.date_end else None,
                'state': rec.state,
                'is_valid': rec.is_valid_at(eval_dt),
            } for rec in records]
        }
