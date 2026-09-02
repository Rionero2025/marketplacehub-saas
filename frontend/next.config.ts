import type { NextConfig } from "next";

const hostPort = (process.env.MARKETPLACE_HUB_API_HOSTPORT || "").trim();
const configuredOrigin = (process.env.MARKETPLACE_HUB_API_INTERNAL_URL || "").trim();
const apiOrigin = (
  configuredOrigin || (hostPort ? `http://${hostPort}` : "http://127.0.0.1:8000")
).replace(/\/$/, "");

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${apiOrigin}/api/:path*` },
      { source: "/health", destination: `${apiOrigin}/health` },
      { source: "/ready", destination: `${apiOrigin}/ready` },
    ];
  },
};

export default nextConfig;
