FROM odoo:19.0

USER root

# Copiar e instalar dependencias de Python desde requirements.txt
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --break-system-packages --ignore-installed -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

# Asegurar directorios de addons personalizados y persistencia de modelos IA en el volumen /var/lib/odoo
ENV ODOO_AI_MODELS_DIR=/var/lib/odoo/ai_models
ENV HF_HOME=/var/lib/odoo/ai_models/hf_cache
ENV SENTENCE_TRANSFORMERS_HOME=/var/lib/odoo/ai_models/st_cache
RUN mkdir -p /mnt/extra-addons/custom /var/lib/odoo/ai_models/models /var/lib/odoo/ai_models/hf_cache /var/lib/odoo/ai_models/st_cache \
    && chown -R odoo:odoo /mnt/extra-addons /var/lib/odoo

USER odoo



