import type { NextConfig } from "next";

// The local LangGraph server (127.0.0.1:2024) is reached through this server, same
// origin: /langgraph/* is the agent API (streamed), /workbench/* the replays.
const nextConfig: NextConfig = {
  // no compression: it would buffer the agent's event stream (SSE)
  compress: false,
  async rewrites() {
    return [
      {
        source: "/langgraph/:path*",
        destination: "http://127.0.0.1:2024/:path*",
      },
      {
        source: "/workbench/:path*",
        destination: "http://127.0.0.1:2024/workbench/:path*",
      },
    ];
  },
};

export default nextConfig;
