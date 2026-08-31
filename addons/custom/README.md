# Custom Addons

Este directorio contiene los módulos de Odoo desarrollados internamente para **Spark ERP**.

## Convención de desarrollo:
- Cada módulo debe incluir su correspondiente archivo `__manifest__.py`.
- Registrar cada nuevo módulo en `config/modules.list`.
- Los módulos colocados aquí son montados automáticamente en `/mnt/extra-addons/custom` dentro del contenedor Odoo.
