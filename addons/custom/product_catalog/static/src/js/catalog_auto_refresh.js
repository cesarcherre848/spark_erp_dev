/** @odoo-module **/

import { onMounted, onWillUnmount } from "@odoo/owl";
import { patch } from "@web/core/utils/patch";
import { FormController } from "@web/views/form/form_controller";
import { ListController } from "@web/views/list/list_controller";

patch(FormController.prototype, {
    setup() {
        super.setup(...arguments);
        if (this.props.resModel === "product.catalog" || this.props.resModel === "res.partner") {
            this._catalogRefreshTimer = null;
            this._isCatalogRefreshing = false;

            onMounted(() => {
                this._catalogRefreshTimer = setInterval(async () => {
                    if (this._isCatalogRefreshing) {
                        return;
                    }
                    try {
                        const root = this.model?.root;
                        if (!root) {
                            return;
                        }
                        const isDirty = typeof root.isDirty === "function" ? await root.isDirty() : Boolean(root.dirty);
                        if (isDirty) {
                            return;
                        }

                        let shouldRefresh = false;
                        if (this.props.resModel === "product.catalog") {
                            shouldRefresh = root.data?.index_state === "processing";
                        } else if (this.props.resModel === "res.partner") {
                            const catalogList = root.data?.catalog_ids?.records || [];
                            shouldRefresh = catalogList.some((r) => r.data?.index_state === "processing");
                        }

                        if (shouldRefresh) {
                            this._isCatalogRefreshing = true;
                            await root.load();
                            this.render(true);
                        }
                    } catch {
                        // Ignorar errores transitorios de red durante el polling
                    } finally {
                        this._isCatalogRefreshing = false;
                    }
                }, 3000);
            });

            onWillUnmount(() => {
                if (this._catalogRefreshTimer) {
                    clearInterval(this._catalogRefreshTimer);
                    this._catalogRefreshTimer = null;
                }
            });
        }
    },
});

patch(ListController.prototype, {
    setup() {
        super.setup(...arguments);
        if (this.props.resModel === "product.catalog") {
            this._catalogListRefreshTimer = null;
            this._isCatalogListRefreshing = false;

            onMounted(() => {
                this._catalogListRefreshTimer = setInterval(async () => {
                    if (this._isCatalogListRefreshing) {
                        return;
                    }
                    try {
                        const root = this.model?.root;
                        if (!root || root.editedRecord) {
                            return;
                        }
                        const records = root.records || [];
                        const hasProcessing = records.some((rec) => rec.data?.index_state === "processing");
                        if (hasProcessing) {
                            this._isCatalogListRefreshing = true;
                            await root.load();
                            this.render(true);
                        }
                    } catch {
                        // Ignorar errores transitorios
                    } finally {
                        this._isCatalogListRefreshing = false;
                    }
                }, 4000);
            });

            onWillUnmount(() => {
                if (this._catalogListRefreshTimer) {
                    clearInterval(this._catalogListRefreshTimer);
                    this._catalogListRefreshTimer = null;
                }
            });
        }
    },
});
