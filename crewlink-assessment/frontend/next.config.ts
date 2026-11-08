import type { NextConfig } from "next";

// The browser only ever talks to this Next.js server; /api/* is proxied to Django.
// Same origin for the browser -> no CORS setup, and the API address stays server-side.
// Rewrites are baked in at build time, so Docker passes API_URL as a build arg.
const API_URL = process.env.API_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  // Docker builds a small standalone server (node server.js, no node_modules);
  // locally, plain `npm run dev` / `npm run start` work as usual.
  output: process.env.STANDALONE === "1" ? "standalone" : undefined,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_URL}/api/:path*` }];
  },
};

export default nextConfig;
