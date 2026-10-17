# -*- coding: utf-8 -*-
from odoo import models, fields, api


class ResPartner(models.Model):
    _inherit = 'res.partner'

    catalog_ids = fields.One2many(
        'product.catalog',
        'partner_id',
        string='Catálogos de Productos',
    )
    catalog_count = fields.Integer(
        string='Cant. Catálogos',
        compute='_compute_catalog_count',
    )

    @api.depends('catalog_ids')
    def _compute_catalog_count(self):
        for partner in self:
            partner.catalog_count = len(partner.catalog_ids)

    def action_view_catalogs(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'product_catalog.action_product_catalog'
        )
        action['domain'] = [('partner_id', '=', self.id)]
        action['context'] = {
            'default_partner_id': self.id,
            'default_supplier_rank': 1,
            'res_partner_search_mode': 'supplier',
        }
        return action
