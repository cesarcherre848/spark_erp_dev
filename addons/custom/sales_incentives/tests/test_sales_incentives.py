# -*- coding: utf-8 -*-
from datetime import timedelta
from odoo import fields
from odoo.tests.common import TransactionCase
from odoo.exceptions import ValidationError


class TestSalesIncentives(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env['res.partner'].create({
            'name': 'Proveedor Mayorista Test S.A.',
            'email': 'proveedor@test.com',
            'vat': 'PRV987654321',
            'supplier_rank': 1,
        })
        cls.now = fields.Datetime.now()
        cls.tag_c10 = cls.env['product.tag'].create({
            'name': 'Catálogo C10',
            'color': '1',
        })
        cls.tag_c11 = cls.env['product.tag'].create({
            'name': 'Catálogo C11',
            'color': '2',
        })

    def test_01_create_incentive_indefinite_success(self):
        """Prueba creación exitosa con inicio obligatorio y fin nulo (indefinido)."""
        incentive = self.env['sale.incentive'].create({
            'partner_id': self.partner.id,
            'title': 'Plan Premium 20%',
            'description': 'Descuento especial del 20% en toda la línea cosmética.',
            'observations': 'Aplica para órdenes superiores a S/ 500.',
            'date_start': self.now - timedelta(days=1),
            'date_end': False,
        })
        self.assertTrue(incentive.id)
        self.assertEqual(incentive.state, 'active')
        self.assertTrue(incentive.is_valid)
        self.assertEqual(incentive.title, 'Plan Premium 20%')
        self.assertEqual(incentive.description, 'Descuento especial del 20% en toda la línea cosmética.')
        self.assertEqual(incentive.observations, 'Aplica para órdenes superiores a S/ 500.')
        self.assertIn('Plan Premium 20%', incentive.name)
        self.assertIn('Proveedor Mayorista Test S.A.', incentive.name)

    def test_02_constraint_date_end_before_start(self):
        """Verifica que no permita guardar fecha fin anterior a la fecha inicio."""
        with self.assertRaises(ValidationError):
            self.env['sale.incentive'].create({
                'partner_id': self.partner.id,
                'title': 'Plan Erróneo',
                'date_start': self.now,
                'date_end': self.now - timedelta(days=5),
            })

    def test_03_states_upcoming_active_expired(self):
        """Verifica el cálculo de los 3 estados temporales."""
        # 1. Por iniciar (futuro)
        upcoming = self.env['sale.incentive'].create({
            'partner_id': self.partner.id,
            'title': 'Plan Futuro',
            'date_start': self.now + timedelta(days=5),
            'date_end': self.now + timedelta(days=10),
        })
        self.assertEqual(upcoming.state, 'upcoming')
        self.assertFalse(upcoming.is_valid)

        # 2. Vigente (activo)
        active = self.env['sale.incentive'].create({
            'partner_id': self.partner.id,
            'title': 'Plan Actual',
            'date_start': self.now - timedelta(days=2),
            'date_end': self.now + timedelta(days=2),
        })
        self.assertEqual(active.state, 'active')
        self.assertTrue(active.is_valid)

        # 3. Vencido (pasado)
        expired = self.env['sale.incentive'].create({
            'partner_id': self.partner.id,
            'title': 'Plan Antiguo',
            'date_start': self.now - timedelta(days=20),
            'date_end': self.now - timedelta(days=10),
        })
        self.assertEqual(expired.state, 'expired')
        self.assertFalse(expired.is_valid)

    def test_04_partner_relation_and_count(self):
        """Verifica que el partner tenga acceso a sus incentivos y compute el contador."""
        initial_count = self.partner.incentive_count
        inc1 = self.env['sale.incentive'].create({
            'partner_id': self.partner.id,
            'title': 'Beneficio A',
            'date_start': self.now,
        })
        inc2 = self.env['sale.incentive'].create({
            'partner_id': self.partner.id,
            'title': 'Beneficio B',
            'date_start': self.now,
        })

        self.assertEqual(self.partner.incentive_count, initial_count + 2)
        self.assertIn(inc1, self.partner.incentive_ids)
        self.assertIn(inc2, self.partner.incentive_ids)

    def test_05_is_valid_at_helper(self):
        """Verifica la lógica temporal del helper is_valid_at."""
        inc = self.env['sale.incentive'].create({
            'partner_id': self.partner.id,
            'title': 'Plan Específico',
            'date_start': self.now - timedelta(days=10),
            'date_end': self.now + timedelta(days=10),
        })
        # Válido hoy
        self.assertTrue(inc.is_valid_at(self.now))
        # No válido antes de inicio
        self.assertFalse(inc.is_valid_at(self.now - timedelta(days=15)))
        # No válido después de fin
        self.assertFalse(inc.is_valid_at(self.now + timedelta(days=15)))

    def test_06_incentive_tags(self):
        """Verifica la asignación, persistencia y búsqueda por etiquetas (product.tag)."""
        inc_c10 = self.env['sale.incentive'].create({
            'partner_id': self.partner.id,
            'title': 'Bono Especial Catálogo C10',
            'date_start': self.now,
            'tag_ids': [(6, 0, [self.tag_c10.id])],
        })
        inc_both = self.env['sale.incentive'].create({
            'partner_id': self.partner.id,
            'title': 'Bono Bicampaña C10 + C11',
            'date_start': self.now,
            'tag_ids': [(6, 0, [self.tag_c10.id, self.tag_c11.id])],
        })
        inc_none = self.env['sale.incentive'].create({
            'partner_id': self.partner.id,
            'title': 'Plan Sin Etiquetas',
            'date_start': self.now,
        })

        # Comprobar tags asignados
        self.assertIn(self.tag_c10, inc_c10.tag_ids)
        self.assertEqual(len(inc_c10.tag_ids), 1)

        self.assertIn(self.tag_c10, inc_both.tag_ids)
        self.assertIn(self.tag_c11, inc_both.tag_ids)
        self.assertEqual(len(inc_both.tag_ids), 2)

        self.assertEqual(len(inc_none.tag_ids), 0)

        # Comprobar búsqueda por tag_ids
        search_c10 = self.env['sale.incentive'].search([('tag_ids', 'in', [self.tag_c10.id])])
        self.assertIn(inc_c10, search_c10)
        self.assertIn(inc_both, search_c10)
        self.assertNotIn(inc_none, search_c10)

        search_c11 = self.env['sale.incentive'].search([('tag_ids', 'in', [self.tag_c11.id])])
        self.assertNotIn(inc_c10, search_c11)
        self.assertIn(inc_both, search_c11)
        self.assertNotIn(inc_none, search_c11)

