# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class SaleIncentive(models.Model):
    _name = 'sale.incentive'
    _description = 'Plan de Incentivos de Proveedores'
    _order = 'date_start desc, id desc'

    name = fields.Char(
        string='Referencia',
        compute='_compute_name',
        store=True,
        index=True
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Proveedor',
        domain="[('supplier_rank', '>', 0)]",
        required=True,
        ondelete='cascade',
        index=True,
        help='Proveedor o fabricante asociado al plan de incentivos/beneficios.'
    )
    title = fields.Char(
        string='Título',
        required=True,
        index=True,
        help='Título o encabezado identificador del incentivo.'
    )
    description = fields.Text(
        string='Descripción',
        help='Detalle de lo que incluye el beneficio, especificaciones de productos o premios.'
    )
    observations = fields.Text(
        string='Observaciones',
        help='Condiciones comerciales, restricciones de entrega o requisitos de calificación.'
    )
    date_start = fields.Datetime(
        string='Fecha Inicio',
        required=True,
        default=fields.Datetime.now,
        index=True,
        help='Fecha y hora a partir de la cual entra en vigencia el beneficio.'
    )
    date_end = fields.Datetime(
        string='Fecha Fin',
        index=True,
        help='Fecha y hora de finalización de la vigencia (opcional).'
    )
    active = fields.Boolean(
        string='Activo',
        default=True,
        help='Permite archivar o desactivar este plan sin eliminarlo.'
    )
    company_id = fields.Many2one(
        'res.company',
        string='Compañía',
        default=lambda self: self.env.company,
        required=True
    )
    state = fields.Selection([
        ('upcoming', 'Por Iniciar'),
        ('active', 'Vigente'),
        ('expired', 'Vencido')
    ], string='Estado de Vigencia', compute='_compute_state', search='_search_state')

    is_valid = fields.Boolean(
        string='Es Válido Ahora',
        compute='_compute_is_valid'
    )

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
            if not rec.date_start:
                rec.state = 'upcoming'
            elif rec.date_start > now:
                rec.state = 'upcoming'
            elif rec.date_end and rec.date_end < now:
                rec.state = 'expired'
            else:
                rec.state = 'active'

    @api.depends('state')
    def _compute_is_valid(self):
        for rec in self:
            rec.is_valid = (rec.state == 'active')

    def _search_state(self, operator, value):
        now = fields.Datetime.now()
        if operator in ('=', '!='):
            if value == 'active':
                domain = [
                    ('date_start', '<=', now),
                    '|',
                    ('date_end', '=', False),
                    ('date_end', '>=', now)
                ]
            elif value == 'upcoming':
                domain = [('date_start', '>', now)]
            elif value == 'expired':
                domain = [
                    ('date_end', '!=', False),
                    ('date_end', '<', now)
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

    def is_valid_at(self, check_datetime):
        """Determina si el incentivo está vigente en un datetime específico."""
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
