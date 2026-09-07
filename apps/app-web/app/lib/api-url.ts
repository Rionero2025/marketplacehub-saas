const configuredApiUrl =
  process.env.INTERNAL_API_URL ??
  process.env.MARKETPLACE_HUB_API_INTERNAL_URL ??
  process.env.MARKETPLACE_HUB_API_HOSTPORT ??
  "http://localhost:8000";

export const apiUrl = configuredApiUrl.includes("://")
  ? configuredApiUrl.replace(/\/$/, "")
  : `http://${configuredApiUrl.replace(/\/$/, "")}`;
