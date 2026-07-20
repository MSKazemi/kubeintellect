import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // When deployed inside talktokube, set NEXT_PUBLIC_BASE_PATH=/kq
  // When deployed standalone, leave unset
  basePath: process.env.NEXT_PUBLIC_BASE_PATH ?? "",

  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          {
            key: "Content-Security-Policy",
            value: "frame-ancestors *",
          },
          {
            key: "X-Frame-Options",
            value: "ALLOWALL",
          },
        ],
      },
    ];
  },
};

export default nextConfig;
