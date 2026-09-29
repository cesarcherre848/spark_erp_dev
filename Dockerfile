FROM odoo:19.0

USER root

# Asegurar directorios de addons personalizados y permisos
RUN mkdir -p /mnt/extra-addons/custom \
    && chown -R odoo:odoo /mnt/extra-addons

USER odoo
