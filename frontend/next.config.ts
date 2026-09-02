import type { NextConfig } from "next";

const apiOrigin = (process.env.MARKETPLACE_HUB_API_INTERNAL_URL || "http://127.0.0.1:8000").replace(/\/$/, "");

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${apiOrigin}/api/:path*` },
      { source: "/health", destination: `${apiOrigin}/health` },
    ];
  },
};

export default nextConfig;
