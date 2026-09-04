# Test locali da valutare per il porting

Fonte: installazione locale Marketplace Hub VERSION 271, cartella tests; inventario del 4 settembre 2026.

Classificazione preliminare per dominio ricavata dai nomi dei file. Nessuno di questi 116 file è stato importato o eseguito in questa PR. La presenza di un test non certifica che sia attuale o indipendente da dati locali. Le appartenenze trasversali sono assegnate a un solo gruppo per non duplicare i conteggi.

| Dominio | File |
|---|---:|
| Fornitori e ordini fornitore | 8 |
| Contabilità e margini | 11 |
| Cataloghi, AI e tassonomie | 13 |
| Database, runtime e interfaccia legacy | 13 |
| Dashboard e statistiche | 6 |
| Marketplace, ordini e Buy Box | 23 |
| Assistenza | 3 |
| Spedizioni, tracking e documenti | 39 |

Prima di portare ogni gruppo: leggere fixture e dipendenze, eliminare accessi a dati/credenziali reali, distinguere prove di algoritmo da asserzioni sul testo Streamlit, adattare solo i confini di storage/tenant e confrontare i risultati. I moduli locali di localizzazione/enrichment v259/v261 richiedono anche la verifica dei chiamanti.

Ordine proposto: casi contabilità/costi pertinenti alla PR 2; prove database e tenant per la PR 3; ordini per la PR 4; cataloghi per la PR 5. Gli altri gruppi accompagnano il porting delle rispettive funzioni. Non sono esclusi dalla migrazione.

## Fornitori e ordini fornitore

- `test_abonline.py`
- `test_cecotec_countries.py`
- `test_cecotec_orders.py`
- `test_forcetop.py`
- `test_hurtel.py`
- `test_innpro_full_feed_v249.py`
- `test_innpro_orders_v262.py`
- `test_innpro_orders_v263.py`

## Contabilità e margini

- `test_accounting.py`
- `test_accounting_catalog_selection_v257.py`
- `test_accounting_costs.py`
- `test_accounting_incremental_sync.py`
- `test_accounting_live_manual_v259.py`
- `test_accounting_pdf.py`
- `test_accounting_review_seller_selector_v268.py`
- `test_innpro_accounting.py`
- `test_innpro_accounting_light_v256.py`
- `test_kaufland_profit.py`
- `test_profit_sharing.py`

## Cataloghi, AI e tassonomie

- `test_ai_providers.py`
- `test_catalog_ai_v258.py`
- `test_catalog_feed_completeness_v249.py`
- `test_catalog_intelligence_v244.py`
- `test_catalog_intelligence_v245.py`
- `test_catalog_localization_v259.py`
- `test_catalog_normalization_v249.py`
- `test_catalog_normalization_v250.py`
- `test_catalog_normalization_v252.py`
- `test_catalog_product_enrichment_v261.py`
- `test_catalog_publication_v246.py`
- `test_kaufland_category_decide_v248.py`
- `test_kaufland_category_schema_v247.py`

## Database, runtime e interfaccia legacy

- `test_app_navigation_v236.py`
- `test_batch_memory.py`
- `test_data_transfer_v260.py`
- `test_data_transfer_v261.py`
- `test_db_lock_retry.py`
- `test_db_storage_permissions.py`
- `test_postgresql_backend.py`
- `test_release_consistency_v241.py`
- `test_sqlite_concurrency_v251.py`
- `test_startup_auto_update_v232.py`
- `test_startup_runtime_v238.py`
- `test_startup_stale_cleanup_v254.py`
- `test_wysiwyg.py`

## Dashboard e statistiche

- `test_dashboard.py`
- `test_dashboard_clickable_v264.py`
- `test_product_stats_review_ui_v266.py`
- `test_product_stats_review_v266.py`
- `test_product_stats_ui_v265.py`
- `test_product_stats_v265.py`

## Marketplace, ordini e Buy Box

- `test_deletion_scope.py`
- `test_kaufland_bulk_deletion.py`
- `test_kaufland_buybox.py`
- `test_kaufland_buybox_account_v203.py`
- `test_kaufland_buybox_fast_v242.py`
- `test_kaufland_deletion_cached_snapshot_v243.py`
- `test_kaufland_deletion_page_v187.py`
- `test_kaufland_history.py`
- `test_kaufland_inventory_client.py`
- `test_kaufland_inventory_deletion.py`
- `test_kaufland_live_inventory.py`
- `test_kaufland_offer.py`
- `test_kaufland_order_costs.py`
- `test_kaufland_orders.py`
- `test_kaufland_price_update.py`
- `test_kaufland_views.py`
- `test_marketplace_deletion.py`
- `test_marketplace_order_states.py`
- `test_marketplace_order_states_v177.py`
- `test_marketplace_order_states_v180.py`
- `test_order_selection.py`
- `test_worten.py`
- `test_worten_views.py`

## Assistenza

- `test_kaufland_support.py`
- `test_support_connectors_v160.py`
- `test_support_hub.py`

## Spedizioni, tracking e documenti

- `test_order_tracking.py`
- `test_packlink.py`
- `test_packlink_auto_best_v270.py`
- `test_packlink_csv_compat_v271.py`
- `test_packlink_csv_v239.py`
- `test_packlink_csv_v240_grid_recipient.py`
- `test_packlink_install_sync_v213.py`
- `test_packlink_order_import_v212.py`
- `test_packlink_order_package_persistence_v220.py`
- `test_packlink_order_recovery_v216.py`
- `test_packlink_package_memory_v212.py`
- `test_packlink_page_v208.py`
- `test_packlink_page_v214.py`
- `test_packlink_postal_formats_v240.py`
- `test_packlink_recipient_contacts_v269.py`
- `test_packlink_sender_addresses_v209.py`
- `test_packlink_v217_order_service_compat.py`
- `test_packlink_v218.py`
- `test_packlink_v220.py`
- `test_packlink_v221.py`
- `test_packlink_v223.py`
- `test_packlink_v224_alignment.py`
- `test_packlink_v225_no_extra_confirmation.py`
- `test_packlink_v226_address_hydration.py`
- `test_packlink_v227_regeneration_tariffs.py`
- `test_packlink_v228_complete_draft_locations.py`
- `test_packlink_v229_official_warehouse_draft.py`
- `test_packlink_v230_fresh_destination.py`
- `test_packlink_v231_existing_shipping_scope.py`
- `test_packlink_v233_official_integration_registration.py`
- `test_packlink_v235_destination_selector.py`
- `test_packlink_v237_self_heal.py`
- `test_packlink_v253.py`
- `test_packlink_v255_order_cache.py`
- `test_shipping_deadlines.py`
- `test_supplier_documents.py`
- `test_tracking_file_archive.py`
- `test_tracking_shipping.py`
- `test_worten_tracking_api.py`
