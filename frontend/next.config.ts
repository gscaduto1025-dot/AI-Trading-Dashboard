import type { NextConfig } from "next";

// The browser talks to Next.js only; /api/* is proxied to the FastAPI backend.
const backendUrl = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  cacheComponents: true,
  experimental: {
    // The default proxy timeout (30 s) is shorter than a cold-start backend request
    // (~90-110 s while the Alpaca request quota is busy), which surfaced as
    // "socket hang up" errors on first load.
    proxyTimeout: 300_000,
  },
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backendUrl}/api/:path*` }];
  },
};

export default nextConfig;
