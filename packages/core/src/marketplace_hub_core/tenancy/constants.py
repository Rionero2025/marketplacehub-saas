from uuid import NAMESPACE_URL, uuid5

PLATFORM_ID = uuid5(NAMESPACE_URL, "https://marketplacehub.internal/platform")
ROLE_LABELS = {
    "PLATFORM_OWNER": "Proprietario piattaforma",
    "PLATFORM_ADMIN": "Amministratore piattaforma",
    "PLATFORM_SUPPORT": "Assistenza piattaforma",
    "AGENCY_OWNER": "Proprietario agenzia",
    "AGENCY_ADMIN": "Amministratore agenzia",
    "AGENCY_USER": "Collaboratore agenzia",
    "SELLER_OWNER": "Proprietario Seller",
    "SELLER_ADMIN": "Amministratore Seller",
    "SELLER_USER": "Collaboratore Seller",
}
PERMISSION_LABELS = {
    "ACCOUNTING": "Contabilità",
    "LOGISTICS": "Logistica",
    "CATALOG": "Catalogo",
    "CUSTOMER_SERVICE": "Assistenza clienti",
    "MARKETING": "Marketing",
    "VIEW_ONLY": "Sola lettura",
    "WORKSPACE_VIEW": "Consulta il negozio",
    "WORKSPACE_MANAGE": "Modifica anagrafica negozio",
}
MANAGEMENT_ROLES = frozenset(code for code in ROLE_LABELS if code.endswith(("_OWNER", "_ADMIN")))
ALL_PERMISSIONS = frozenset(PERMISSION_LABELS) - {"VIEW_ONLY"}
