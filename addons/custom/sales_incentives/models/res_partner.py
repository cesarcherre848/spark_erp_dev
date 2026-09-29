# -*- coding: utf-8 -*-
from odoo import models, fields, api


class ResPartner(models.Model):
    _inherit = 'res.partner'

    incentive_ids = fields.One2many(
        'sale.incentive',
        'partner_id',
        string='Planes de Incentivos de Proveedor'
    )
    incentive_count = fields.Integer(
        string='Cant. Incentivos',
        compute='_compute_incentive_count'
    )

    @api.depends('incentive_ids')
    def _compute_incentive_count(self):
        for partner in self:
            partner.incentive_count = len(partner.incentive_ids)

    def action_view_incentives(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('sales_incentives.action_sale_incentive')
        action['domain'] = [('partner_id', '=', self.id)]
        action['context'] = {
            'default_partner_id': self.id,
            'default_supplier_rank': 1,
            'res_partner_search_mode': 'supplier',
        }
        return action
