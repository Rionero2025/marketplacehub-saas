import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  transpilePackages: ["@marketplace-hub/ui"],
};

export default nextConfig;
