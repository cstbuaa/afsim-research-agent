import type { NextConfig } from "next";

// /workbench/* (replays) is served by the local LangGraph server next to the agent
const nextConfig: NextConfig = {
  async rewrites() {
    return [
      {
        source: "/workbench/:path*",
        destination: "http://127.0.0.1:2024/workbench/:path*",
      },
    ];
  },
};

export default nextConfig;
