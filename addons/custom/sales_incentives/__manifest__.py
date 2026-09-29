# -*- coding: utf-8 -*-
{
    'name': 'Sales Incentives',
    'version': '1.0.0',
    'category': 'Sales/Sales',
    'summary': 'Gestión de planes de incentivos y beneficios por cliente con vigencia temporal',
    'description': """
Sales Incentives
================
Módulo para la asignación y control de planes de incentivos/beneficios por cliente (res.partner).

Características principales:
- Asociación de un cliente (res.partner) con su plan de beneficios (string).
- Control de vigencia temporal con fecha/hora de inicio (obligatoria) y fin (opcional).
- Cálculo dinámico de estado de vigencia (Por Iniciar, Vigente, Vencido).
- Vistas dedicadas dentro del módulo de Ventas (Ventas > Pedidos > Planes de Incentivos).
- Integración en la vista de cliente (res.partner) con pestaña editable y botón inteligente (Smart Button).
- API REST (/api/v1/sales_incentives) para consulta de planes por cliente y fecha de vigencia.
    """,
    'author': 'Spark ERP',
    'depends': ['base', 'sale'],
    'data': [
        'security/ir.model.access.csv',
        'views/sale_incentive_views.xml',
        'views/res_partner_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
